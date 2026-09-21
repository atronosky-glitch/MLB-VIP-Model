"""Tests for the sportsbook bet-slip deep-link capture and lookup that
power the customer-facing "Bet Now" button.

- src/odds_api_props_parser.py::_build_prop_row captures bet_link from
  The Odds API's includeLinks=true response (outcome -> market ->
  bookmaker fallback, confirmed live 2026-09-15).
- database/db_manager.py::save_player_prop_batch persists it.
- database/db_manager.py::get_bet_links looks up the freshest link per
  (event_id, player_id, market_type, line, side, sportsbook) key.
"""

from src.odds_api_props_parser import _build_prop_row
from database.db_manager import save_player_prop_batch, get_bet_links


def _outcome(**overrides):
    base = {"name": "Over", "description": "Aaron Judge", "price": -110, "point": 1.5}
    base.update(overrides)
    return base


class TestBuildPropRowBetLink:
    def test_bet_link_passed_through_when_present(self):
        row = _build_prop_row(
            event_id="E1", book_name="draftkings", book_last_update=None,
            market_type="batting_totalBases_ou", outcome=_outcome(),
            bet_link="https://sportsbook.draftkings.com/?outcomes=abc123",
            canonical_id="p1", display_name="Aaron Judge", confidence="HIGH", method="exact",
        )
        assert row["bet_link"] == "https://sportsbook.draftkings.com/?outcomes=abc123"

    def test_bet_link_none_when_not_provided(self):
        row = _build_prop_row(
            event_id="E1", book_name="novig", book_last_update=None,
            market_type="batting_totalBases_ou", outcome=_outcome(),
            canonical_id="p1", display_name="Aaron Judge", confidence="HIGH", method="exact",
        )
        assert row["bet_link"] is None


class TestSavePlayerPropBatchBetLink:
    def _row(self, **overrides):
        row = {
            "event_id": "E1", "odd_id": "o1", "sportsbook": "draftkings",
            "player_id": "p1", "player_name": "Aaron Judge", "team_id": "", "team_name": "",
            "market_type": "batting_totalBases_ou", "market_group_key": "E1|p1|1.5|0|OVER",
            "side": "OVER", "line": 1.5, "price": -110, "decimal_odds": 1.909,
            "is_alt_line": 0, "available": 1, "validation_status": "VALID",
            "mapping_confidence": "HIGH", "mapping_method": "exact", "validation_reason": "OK",
            "captured_at": "2026-09-15T12:00:00+00:00",
        }
        row.update(overrides)
        return row

    def test_bet_link_persists(self, db_conn):
        save_player_prop_batch(db_conn, [self._row(bet_link="https://sportsbook.draftkings.com/?outcomes=abc123")])
        row = db_conn.execute("SELECT bet_link FROM player_prop_odds WHERE event_id='E1'").fetchone()
        assert dict(row)["bet_link"] == "https://sportsbook.draftkings.com/?outcomes=abc123"

    def test_row_without_bet_link_key_defaults_to_null_not_a_crash(self, db_conn):
        """A row built by a caller that predates this feature (or the
        game-odds path, which doesn't have bet_link support yet) must
        still insert cleanly."""
        row = self._row()
        assert "bet_link" not in row
        save_player_prop_batch(db_conn, [row])
        result = db_conn.execute("SELECT bet_link FROM player_prop_odds WHERE event_id='E1'").fetchone()
        assert dict(result)["bet_link"] is None


class TestSavePlayerPropBatchLeague:
    """Real bug found live in production (2026-09-21): league was never
    in save_player_prop_batch's INSERT column list at all, so every row
    silently fell back to the column's own DEFAULT 'MLB' regardless of
    which league's scan produced it -- confirmed live via real NFL
    player props stored with league='MLB'. src/player_prop_scanner.py
    now stamps the real league onto every row before calling this; the
    setdefault inside save_player_prop_batch is only a safety net for
    any other caller that doesn't."""

    def _row(self, **overrides):
        row = {
            "event_id": "E1", "odd_id": "o1", "sportsbook": "draftkings",
            "player_id": "p1", "player_name": "Test Player", "team_id": "", "team_name": "",
            "market_type": "receiving_yards_ou", "market_group_key": "E1|p1|79.5|0|OVER",
            "side": "OVER", "line": 79.5, "price": -110, "decimal_odds": 1.909,
            "is_alt_line": 0, "available": 1, "validation_status": "VALID",
            "mapping_confidence": "HIGH", "mapping_method": "exact", "validation_reason": "OK",
            "captured_at": "2026-09-21T12:00:00+00:00",
        }
        row.update(overrides)
        return row

    def test_explicit_league_persists(self, db_conn):
        save_player_prop_batch(db_conn, [self._row(league="NFL")])
        row = db_conn.execute("SELECT league FROM player_prop_odds WHERE event_id='E1'").fetchone()
        assert dict(row)["league"] == "NFL"

    def test_row_without_league_key_defaults_to_mlb_not_a_crash(self, db_conn):
        """Safety-net default for a caller that doesn't set it -- not
        the primary fix (that's player_prop_scanner.py always setting
        it explicitly), just proof this doesn't raise."""
        row = self._row()
        assert "league" not in row
        save_player_prop_batch(db_conn, [row])
        result = db_conn.execute("SELECT league FROM player_prop_odds WHERE event_id='E1'").fetchone()
        assert dict(result)["league"] == "MLB"

    def test_different_leagues_in_the_same_batch_each_persist_correctly(self, db_conn):
        save_player_prop_batch(db_conn, [
            self._row(event_id="E1", league="NFL"),
            self._row(event_id="E2", odd_id="o2", league="MLB"),
        ])
        rows = {
            dict(r)["event_id"]: dict(r)["league"]
            for r in db_conn.execute("SELECT event_id, league FROM player_prop_odds").fetchall()
        }
        assert rows == {"E1": "NFL", "E2": "MLB"}


class TestGetBetLinks:
    def _insert(self, conn, **overrides):
        row = {
            "event_id": "E1", "odd_id": "o1", "sportsbook": "draftkings",
            "player_id": "p1", "player_name": "Aaron Judge", "team_id": "", "team_name": "",
            "market_type": "batting_totalBases_ou", "market_group_key": "E1|p1|1.5|0|OVER",
            "side": "OVER", "line": 1.5, "price": -110, "decimal_odds": 1.909,
            "is_alt_line": 0, "available": 1, "validation_status": "VALID",
            "mapping_confidence": "HIGH", "mapping_method": "exact", "validation_reason": "OK",
            "captured_at": "2026-09-15T12:00:00+00:00", "bet_link": None,
        }
        row.update(overrides)
        save_player_prop_batch(conn, [row])

    def _key(self, **overrides):
        base = {
            "event_id": "E1", "player_id": "p1", "market_type": "batting_totalBases_ou",
            "line": 1.5, "side": "OVER", "sportsbook": "draftkings",
        }
        base.update(overrides)
        return (base["event_id"], base["player_id"], base["market_type"], base["line"], base["side"], base["sportsbook"])

    def test_finds_a_matching_link(self, db_conn):
        self._insert(db_conn, bet_link="https://sportsbook.draftkings.com/?outcomes=abc123")
        result = get_bet_links(db_conn, [self._key()])
        assert result[self._key()] == "https://sportsbook.draftkings.com/?outcomes=abc123"

    def test_unmatched_key_is_absent_not_none(self, db_conn):
        self._insert(db_conn, bet_link="https://sportsbook.draftkings.com/?outcomes=abc123")
        other_key = self._key(player_id="someone_else")
        result = get_bet_links(db_conn, [other_key])
        assert other_key not in result

    def test_row_with_null_link_never_returned(self, db_conn):
        self._insert(db_conn, bet_link=None)
        result = get_bet_links(db_conn, [self._key()])
        assert self._key() not in result

    def test_picks_the_freshest_captured_at_among_duplicates(self, db_conn):
        self._insert(db_conn, odd_id="o1", bet_link="https://old-link", captured_at="2026-09-15T10:00:00+00:00")
        self._insert(db_conn, odd_id="o2", bet_link="https://new-link", captured_at="2026-09-15T14:00:00+00:00")
        result = get_bet_links(db_conn, [self._key()])
        assert result[self._key()] == "https://new-link"

    def test_empty_keys_returns_empty_dict(self, db_conn):
        assert get_bet_links(db_conn, []) == {}

    def test_different_sportsbook_is_a_different_key(self, db_conn):
        self._insert(db_conn, sportsbook="draftkings", bet_link="https://dk-link")
        self._insert(db_conn, odd_id="o2", sportsbook="fanduel", bet_link="https://fd-link")
        dk_key = self._key(sportsbook="draftkings")
        fd_key = self._key(sportsbook="fanduel")
        result = get_bet_links(db_conn, [dk_key, fd_key])
        assert result[dk_key] == "https://dk-link"
        assert result[fd_key] == "https://fd-link"
