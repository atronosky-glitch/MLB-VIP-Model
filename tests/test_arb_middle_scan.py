"""Tests for src/arb_middle_scan.py and its worker wiring."""

import inspect
from unittest.mock import patch

import src.worker as worker
from src.arb_middle_scan import run_scan
from database.db_manager import get_active_arbitrage_opportunities, get_active_middle_opportunities


def _insert_odds_row(conn, **overrides):
    row = {
        "event_id": "E1", "odd_id": "odd-1", "sportsbook": "BookA",
        "player_id": "P1", "player_name": "Test Pitcher", "team_id": None, "team_name": None,
        "market_type": "pitching_strikeouts_ou", "market_group_key": "E1|P1|k|6.5",
        "side": "OVER", "line": 6.5, "price": 110, "decimal_odds": 2.10,
        "is_alt_line": 0, "available": 1, "validation_status": "VALID",
        "mapping_confidence": "HIGH", "mapping_method": "exact", "validation_reason": "",
        "captured_at": "2026-09-09T12:00:00+00:00", "league": "MLB",
    }
    row.update(overrides)
    conn.execute(
        """INSERT INTO player_prop_odds
            (event_id, odd_id, sportsbook, player_id, player_name, team_id, team_name,
             market_type, market_group_key, side, line, price, decimal_odds, is_alt_line,
             available, validation_status, mapping_confidence, mapping_method,
             validation_reason, captured_at, league)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (row["event_id"], row["odd_id"], row["sportsbook"], row["player_id"], row["player_name"],
         row["team_id"], row["team_name"], row["market_type"], row["market_group_key"], row["side"],
         row["line"], row["price"], row["decimal_odds"], row["is_alt_line"], row["available"],
         row["validation_status"], row["mapping_confidence"], row["mapping_method"],
         row["validation_reason"], row["captured_at"], row["league"]),
    )
    conn.commit()


class TestRunScan:
    def test_detects_and_persists_a_real_arbitrage(self, db_conn):
        _insert_odds_row(db_conn, sportsbook="BookA", side="OVER", price=110, decimal_odds=2.10, odd_id="o1")
        _insert_odds_row(db_conn, sportsbook="BookB", side="UNDER", price=130, decimal_odds=2.30, odd_id="o2")

        result = run_scan(db_conn, league="MLB", freshness_seconds=10_000_000)

        assert result["arbitrage"]["detected"] == 1
        active = get_active_arbitrage_opportunities(db_conn, "MLB")
        assert len(active) == 1
        assert active[0]["market_type"] == "pitching_strikeouts_ou"

    def test_excludes_spread_and_runline_markets(self, db_conn):
        _insert_odds_row(
            db_conn, sportsbook="BookA", side="HOME", price=150, decimal_odds=2.50,
            market_type="game_spread_ou", market_group_key="E1|spread|-1.5", line=-1.5, odd_id="o1",
        )
        _insert_odds_row(
            db_conn, sportsbook="BookB", side="AWAY", price=150, decimal_odds=2.50,
            market_type="game_spread_ou", market_group_key="E1|spread|-1.5", line=1.5, odd_id="o2",
        )
        result = run_scan(db_conn, league="MLB", freshness_seconds=10_000_000)
        assert result["arbitrage"]["detected"] == 0
        assert get_active_arbitrage_opportunities(db_conn, "MLB") == []

    def test_no_opportunities_is_not_an_error(self, db_conn):
        _insert_odds_row(db_conn, sportsbook="BookA", side="OVER", price=-110, decimal_odds=1.909, odd_id="o1")
        _insert_odds_row(db_conn, sportsbook="BookB", side="UNDER", price=-110, decimal_odds=1.909, odd_id="o2")
        result = run_scan(db_conn, league="MLB", freshness_seconds=10_000_000)
        assert result["arbitrage"]["detected"] == 0
        assert result["middles"]["detected"] == 0

    def test_stale_rows_outside_freshness_window_are_ignored(self, db_conn):
        _insert_odds_row(
            db_conn, sportsbook="BookA", side="OVER", price=110, decimal_odds=2.10,
            captured_at="2020-01-01T00:00:00+00:00", odd_id="o1",
        )
        _insert_odds_row(
            db_conn, sportsbook="BookB", side="UNDER", price=130, decimal_odds=2.30,
            captured_at="2020-01-01T00:00:00+00:00", odd_id="o2",
        )
        result = run_scan(db_conn, league="MLB", freshness_seconds=3600)
        assert result["rows_examined"] == 0
        assert result["arbitrage"]["detected"] == 0


class TestWorkerWiring:
    def test_arb_middle_scan_registered_in_dispatch(self):
        source = inspect.getsource(worker._execute_job)
        assert '"arb-middle-scan"' in source

    def test_one_league_failing_does_not_block_the_others(self, db_conn):
        calls = []

        def fake_run_scan(conn, league="MLB", **kwargs):
            calls.append(league)
            if league == "NFL":
                raise RuntimeError("boom")
            return {"league": league, "rows_examined": 0,
                    "arbitrage": {"detected": 0}, "middles": {"detected": 0}}

        with patch("src.arb_middle_scan.run_scan", side_effect=fake_run_scan):
            result = worker._run_arb_middle_scan(db_conn, config=None)

        assert calls == ["MLB", "NFL", "WNBA"]
        assert result["status"] == "success"
        assert result["results"]["MLB"]["arbitrage"]["detected"] == 0
        assert result["results"]["NFL"] == {"error": True}
        assert result["results"]["WNBA"]["arbitrage"]["detected"] == 0
