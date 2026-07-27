"""Regression tests for participant-mapping validation in odds_parser.

The SportsGameOdds API uses statEntityID + marketName as stable
identifiers to prove which team each entityID maps to.  Consensus-based
sign analysis is used *only* to assign validation statuses (read-only),
never to auto-swap prices.
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.odds_parser import (
    parse_odds,
    _build_participant_map,
    _build_audit,
    _validate_mappings,
)
from src.validation_constants import (
    STATUS_VALID,
    STATUS_POSSIBLE_MAPPING_ERROR,
    STATUS_INVALID_MAPPING,
    STATUS_NONE,
    CONFIDENCE_CONFIRMED,
    CONFIDENCE_HIGH,
    APPROVED_STATUSES,
    REASON_SWAP_SUSPECTED,
    REASON_OK,
)

from tests.fixture_data import (
    TB_TOR_EVENT_ID,
    SF_KC_EVENT_ID,
    tb_tor_event as _tb_tor_event,
    sf_kc_event as _sf_kc_event,
)


# ── ParsedOddsResult structure ────────────────────────────────────

class TestParsedOddsResult:
    """Verify parse_odds returns the correct structured result."""

    def test_returns_parsed_odds_result(self, tb_tor_event):
        result = parse_odds(tb_tor_event)
        assert hasattr(result, "odds_rows")
        assert hasattr(result, "audit_rows")
        assert isinstance(result.odds_rows, list)
        assert isinstance(result.audit_rows, list)

    def test_odds_rows_have_validation_fields(self, tb_tor_event):
        result = parse_odds(tb_tor_event)
        for row in result.odds_rows:
            assert "validation_status" in row, f"Row missing validation_status: {row.get('market')}"
            assert "mapping_confidence" in row, f"Row missing mapping_confidence"
            assert "mapping_method" in row, f"Row missing mapping_method"
            assert "validation_reason" in row, f"Row missing validation_reason"
            assert "odd_id" in row, f"Row missing odd_id"

    def test_no_audit_sportsbook_row(self, tb_tor_event):
        """No row with sportsbook='_audit' should exist in odds_rows."""
        result = parse_odds(tb_tor_event)
        audit_rows = [r for r in result.odds_rows if r.get("sportsbook") == "_audit"]
        assert len(audit_rows) == 0, "Found _audit sportsbook row in odds_rows"

    def test_audit_rows_have_expected_fields(self, tb_tor_event):
        result = parse_odds(tb_tor_event)
        for row in result.audit_rows:
            assert "odd_id" in row
            assert "sportsbook" in row
            assert "raw_participant_id" in row
            assert "matched_team_name" in row
            assert "mapping_confidence" in row
            assert "mapping_method" in row
            assert "validation_status" in row


# ── Participant map tests ─────────────────────────────────────────

class TestParticipantMap:
    """Verify _build_participant_map correctly maps entityID → team."""

    def test_away_maps_to_tampa_bay(self, tb_tor_event):
        teams = tb_tor_event.get("teams", {}) or {}
        odds_map = tb_tor_event.get("odds", {}) or {}
        pmap = _build_participant_map(teams, odds_map)
        assert pmap["away"]["name"] == "Tampa Bay Rays"
        assert pmap["away"]["role"] == "away"

    def test_home_maps_to_toronto(self, tb_tor_event):
        teams = tb_tor_event.get("teams", {}) or {}
        odds_map = tb_tor_event.get("odds", {}) or {}
        pmap = _build_participant_map(teams, odds_map)
        assert pmap["home"]["name"] == "Toronto Blue Jays"
        assert pmap["home"]["role"] == "home"

    def test_away_verified_by_team_total_market(self, tb_tor_event):
        teams = tb_tor_event.get("teams", {}) or {}
        odds_map = tb_tor_event.get("odds", {}) or {}
        pmap = _build_participant_map(teams, odds_map)
        verified_by = pmap["away"].get("_verified_by", "")
        assert verified_by, "away mapping not verified by any team-total market"
        assert "-ou-" in verified_by, f"Expected team-total odd ID, got {verified_by}"

    def test_sf_away_maps_to_san_francisco(self, sf_kc_event):
        teams = sf_kc_event.get("teams", {}) or {}
        odds_map = sf_kc_event.get("odds", {}) or {}
        pmap = _build_participant_map(teams, odds_map)
        assert "San Francisco" in pmap["away"]["name"]


# ── Validation-on-every-row tests ─────────────────────────────────

class TestRowLevelValidation:
    """Verify every odds row has a correct validation status attached."""

    def test_betmgm_away_ml_is_possible_mapping_error(self, tb_tor_event):
        """BetMGM's away moneyline must be POSSIBLE_MAPPING_ERROR."""
        result = parse_odds(tb_tor_event)
        betmgm_away = [
            r for r in result.odds_rows
            if r["sportsbook"] == "betmgm" and "-game-ml-away" in r["market"]
        ]
        assert len(betmgm_away) > 0
        for r in betmgm_away:
            assert r["validation_status"] == STATUS_POSSIBLE_MAPPING_ERROR, \
                f"Expected {STATUS_POSSIBLE_MAPPING_ERROR}, got {r['validation_status']}"

    def test_betmgm_home_ml_is_possible_mapping_error(self, tb_tor_event):
        """BetMGM's home moneyline must also be POSSIBLE_MAPPING_ERROR."""
        result = parse_odds(tb_tor_event)
        betmgm_home = [
            r for r in result.odds_rows
            if r["sportsbook"] == "betmgm" and "-game-ml-home" in r["market"]
        ]
        assert len(betmgm_home) > 0
        for r in betmgm_home:
            assert r["validation_status"] == STATUS_POSSIBLE_MAPPING_ERROR, \
                f"Expected {STATUS_POSSIBLE_MAPPING_ERROR}, got {r['validation_status']}"

    def test_fanduel_away_ml_is_valid(self, tb_tor_event):
        """fanduel's away moneyline must be VALID (not flagged)."""
        result = parse_odds(tb_tor_event)
        fd_away = [
            r for r in result.odds_rows
            if r["sportsbook"] == "fanduel" and "-game-ml-away" in r["market"]
        ]
        assert len(fd_away) > 0
        for r in fd_away:
            assert r["validation_status"] == STATUS_VALID, \
                f"Expected VALID, got {r['validation_status']}"

    def test_non_ml_markets_have_valid_status(self, tb_tor_event):
        """Non-moneyline markets (spread, totals) should be VALID."""
        result = parse_odds(tb_tor_event)
        non_ml = [
            r for r in result.odds_rows
            if "-game-ml-" not in r["market"]
               and r.get("is_alt_line") == 0
               and r["validation_status"] != STATUS_VALID
        ]
        assert len(non_ml) == 0, f"Non-ML markets with non-VALID status: {non_ml[:3]}"

    def test_betmgm_price_not_swapped(self, tb_tor_event):
        """Prices must NOT be swapped — BetMGM still has its original negative away price."""
        result = parse_odds(tb_tor_event)
        betmgm_away = [
            r for r in result.odds_rows
            if r["sportsbook"] == "betmgm" and "-game-ml-away" in r["market"]
        ]
        assert len(betmgm_away) > 0
        assert betmgm_away[0]["price"] < 0, \
            "BetMGM away price should still be negative (not swapped)"

    def test_approved_statuses_do_not_include_flagged(self, tb_tor_event):
        """POSSIBLE_MAPPING_ERROR must NOT be in APPROVED_STATUSES."""
        assert STATUS_POSSIBLE_MAPPING_ERROR not in APPROVED_STATUSES
        assert STATUS_INVALID_MAPPING not in APPROVED_STATUSES
        assert STATUS_NONE not in APPROVED_STATUSES

    def test_sf_kc_betmgm_not_flagged(self, sf_kc_event):
        """BetMGM in SF @ KC should NOT be flagged (genuine opinion)."""
        result = parse_odds(sf_kc_event)
        betmgm_flagged = [
            r for r in result.odds_rows
            if r["sportsbook"] == "betmgm"
            and r["validation_status"] == STATUS_POSSIBLE_MAPPING_ERROR
        ]
        assert len(betmgm_flagged) == 0, \
            f"BetMGM incorrectly flagged in SF @ KC: {betmgm_flagged}"

    def test_validation_reason_populated(self, tb_tor_event):
        """Flagged rows should have a reason string."""
        result = parse_odds(tb_tor_event)
        flagged = [
            r for r in result.odds_rows
            if r["validation_status"] != STATUS_VALID
        ]
        for r in flagged:
            assert r["validation_reason"], \
                f"Flagged row missing reason: {r['market']} / {r['sportsbook']}"
        # BetMGM swap should mention swap
        betmgm_flagged = [
            r for r in flagged if r["sportsbook"] == "betmgm"
        ]
        for r in betmgm_flagged:
            assert "swap" in r["validation_reason"].lower()


# ── Audit record tests ────────────────────────────────────────────

class TestAuditRecords:
    """Verify audit records are produced correctly."""

    def test_audit_contains_betmgm_moneyline(self, tb_tor_event):
        result = parse_odds(tb_tor_event)
        betmgm_ml = [
            r for r in result.audit_rows
            if r["sportsbook"] == "betmgm" and "-game-ml-" in r["odd_id"]
        ]
        assert len(betmgm_ml) >= 2, "Expected at least 2 BetMGM ML audit records (away + home)"

    def test_audit_shows_team_name(self, tb_tor_event):
        result = parse_odds(tb_tor_event)
        away_audits = [
            r for r in result.audit_rows
            if "away" in r.get("raw_participant_id", "")
        ]
        for r in away_audits:
            assert "Tampa Bay" in r.get("matched_team_name", ""), \
                f"Expected Tampa Bay, got {r['matched_team_name']}"

    def test_audit_shows_confidence(self, tb_tor_event):
        """Team-specific totals should produce CONFIRMED confidence."""
        result = parse_odds(tb_tor_event)
        confirmed = [
            r for r in result.audit_rows
            if r.get("mapping_confidence") == CONFIDENCE_CONFIRMED
        ]
        assert len(confirmed) > 0, "No CONFIRMED audit records found"
        for r in confirmed:
            assert "marketName verification" in r.get("mapping_method", "")


# ─── Database round-trip tests ────────────────────────────────────

def _setup_db():
    """Create an in-memory database with the odds tables."""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")

    conn.executescript("""
        CREATE TABLE games (event_id TEXT PRIMARY KEY, away_team TEXT, home_team TEXT);

        CREATE TABLE odds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            sportsbook TEXT NOT NULL,
            market TEXT NOT NULL,
            selection TEXT,
            price REAL,
            points REAL,
            is_alt_line INTEGER NOT NULL DEFAULT 0,
            available INTEGER NOT NULL DEFAULT 1,
            pulled_at TEXT NOT NULL DEFAULT (datetime('now')),
            odd_id TEXT DEFAULT '',
            validation_status TEXT DEFAULT 'VALID',
            mapping_confidence TEXT DEFAULT 'NONE',
            mapping_method TEXT DEFAULT '',
            validation_reason TEXT DEFAULT ''
        );

        CREATE TABLE odds_mapping_audit (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            odd_id TEXT NOT NULL,
            sportsbook TEXT NOT NULL,
            raw_participant_id TEXT,
            raw_participant_name TEXT,
            matched_team_id TEXT,
            matched_team_name TEXT,
            mapping_method TEXT,
            mapping_confidence TEXT,
            validation_status TEXT,
            validation_reason TEXT,
            price REAL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    return conn


class TestDatabaseRoundTrip:
    """Verify odds rows and audit rows are stored correctly in SQLite."""

    def test_odds_rows_stored_with_validation_status(self):
        ev = self._get_tb_tor()
        if ev is None:
            pytest.skip("TB @ TOR event not in current cache")
        conn = _setup_db()
        result = parse_odds(ev)
        from database.db_manager import save_odds_batch
        save_odds_batch(conn, result.odds_rows, result.audit_rows)

        row = conn.execute(
            "SELECT validation_status FROM odds WHERE sportsbook='betmgm' AND market LIKE '%-game-ml-away%'"
        ).fetchone()
        assert row is not None
        assert row["validation_status"] == STATUS_POSSIBLE_MAPPING_ERROR

        row2 = conn.execute(
            "SELECT validation_status FROM odds WHERE sportsbook='fanduel' AND market LIKE '%-game-ml-away%'"
        ).fetchone()
        assert row2 is not None
        assert row2["validation_status"] == STATUS_VALID

    def test_audit_rows_stored_in_separate_table(self):
        ev = self._get_tb_tor()
        if ev is None:
            pytest.skip("TB @ TOR event not in current cache")
        conn = _setup_db()
        result = parse_odds(ev)
        from database.db_manager import save_odds_batch
        save_odds_batch(conn, result.odds_rows, result.audit_rows)

        cnt = conn.execute("SELECT COUNT(*) as c FROM odds_mapping_audit").fetchone()["c"]
        assert cnt > 0, "No audit records stored"

        # BetMGM ML audit should exist
        row = conn.execute(
            "SELECT * FROM odds_mapping_audit WHERE sportsbook='betmgm' AND odd_id LIKE '%-game-ml-away%'"
        ).fetchone()
        assert row is not None

    def test_no_audit_sportsbook_in_odds_table(self):
        ev = self._get_tb_tor()
        if ev is None:
            pytest.skip("TB @ TOR event not in current cache")
        conn = _setup_db()
        result = parse_odds(ev)
        from database.db_manager import save_odds_batch
        save_odds_batch(conn, result.odds_rows, result.audit_rows)

        rows = conn.execute(
            "SELECT COUNT(*) as c FROM odds WHERE sportsbook='_audit'"
        ).fetchone()["c"]
        assert rows == 0, "_audit sportsbook found in odds table"

    def test_flagged_rows_still_queryable(self):
        ev = self._get_tb_tor()
        if ev is None:
            pytest.skip("TB @ TOR event not in current cache")
        conn = _setup_db()
        result = parse_odds(ev)
        from database.db_manager import save_odds_batch
        save_odds_batch(conn, result.odds_rows, result.audit_rows)

        rows = conn.execute(
            "SELECT COUNT(*) as c FROM odds WHERE validation_status = ?",
            (STATUS_POSSIBLE_MAPPING_ERROR,),
        ).fetchone()["c"]
        assert rows > 0, "POSSIBLE_MAPPING_ERROR rows not queryable"

    def test_approved_only_query_excludes_flagged(self):
        ev = self._get_tb_tor()
        if ev is None:
            pytest.skip("TB @ TOR event not in current cache")
        conn = _setup_db()
        result = parse_odds(ev)
        from database.db_manager import save_odds_batch
        save_odds_batch(conn, result.odds_rows, result.audit_rows)

        rows = conn.execute(
            """
            SELECT sportsbook, price FROM odds
            WHERE event_id = ? AND is_alt_line = 0 AND available = 1
              AND market LIKE '%-game-ml-away'
              AND validation_status IN ('VALID', 'CONFIRMED', 'VERIFIED')
            ORDER BY sportsbook
            """,
            ("cDV9yci5IGzMCCGu193A",),
        ).fetchall()
        books = {r["sportsbook"] for r in rows}
        assert "betmgm" not in books, \
            f"BetMGM should be excluded from approved query, got: {books}"
        assert "fanduel" in books, "fanduel should be in approved query"

    def test_flagged_cannot_be_best_price(self):
        """POSSIBLE_MAPPING_ERROR rows must not affect best_price."""
        from src.market_analysis import best_price
        approved = [136, 140, 143, 130]
        flagged = [-169]
        # best_price with only approved data
        best = best_price(approved)
        # This should be the best of approved list, NOT the flagged -169
        assert best == 143
        # If we somehow passed flagged prices, they should NOT be picked
        assert best_price(approved + flagged) != -169, \
            "Flagged price should not be best price even if accidentally passed"

    def test_flagged_cannot_affect_consensus(self):
        """Consensus with excluded records must differ from consensus with all records."""
        from src.market_analysis import consensus_price
        # With BetMGM's -169 included
        all_prices = [136, 140, 143, 130, -169]
        # Without BetMGM
        approved_prices = [136, 140, 143, 130]

        all_cons = consensus_price(all_prices)
        approved_cons = consensus_price(approved_prices)

        # The -169 strongly pulls the consensus down
        assert all_cons < approved_cons, \
            f"Flagged prices should pull consensus down: all={all_cons}, approved={approved_cons}"

    def test_transaction_rollback_on_failure(self):
        """If audit insertion fails, odds insertion should also roll back."""
        conn = _setup_db()
        # Insert a game first so we don't fail on FK
        conn.execute("INSERT INTO games (event_id) VALUES ('test_rollback')")
        conn.commit()

        # Use valid odds rows but bad audit rows (missing required fields)
        valid_odds = [{
            "event_id": "test_rollback",
            "sportsbook": "testbook",
            "market": "points-away-game-ml-away",
            "selection": "points-away-game-ml-away",
            "price": -110,
            "points": None,
            "is_alt_line": 0,
            "available": 1,
            "odd_id": "points-away-game-ml-away",
            "validation_status": "VALID",
            "mapping_confidence": "HIGH",
            "mapping_method": "statEntityID",
            "validation_reason": "",
        }]
        bad_audit = [{"bad_key": "value"}]
        from database.db_manager import save_odds_batch
        with pytest.raises(Exception):
            save_odds_batch(conn, valid_odds, bad_audit)

        # Verify nothing was committed
        cnt_odds = conn.execute("SELECT COUNT(*) as c FROM odds").fetchone()["c"]
        cnt_audit = conn.execute("SELECT COUNT(*) as c FROM odds_mapping_audit").fetchone()["c"]
        assert cnt_odds == 0, "Odds rows committed despite audit failure"
        assert cnt_audit == 0, "Audit records committed despite failure"

    @staticmethod
    def _get_tb_tor():
        return dict(_tb_tor_event)
