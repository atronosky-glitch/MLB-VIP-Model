"""Tests for src/execution/market_match_store.py against the db_conn
fixture (full schema via tests/conftest.py, including market_matches)."""

from src.execution.market_match_store import get_existing_match, persist_match
from src.execution.matching import MatchResult


def _result(confidence=0.99, provider_market_id="M1") -> MatchResult:
    return MatchResult(
        recommendation_id="rec-1", provider="kalshi", provider_market_id=provider_market_id,
        confidence=confidence, team_score=1.0, market_type_score=1.0,
        line_score=1.0, date_score=1.0, provider_title="Athletics @ Toronto Blue Jays",
    )


class TestPersistAndGetExistingMatch:
    def test_no_existing_match_returns_none(self, db_conn):
        assert get_existing_match(db_conn, "rec-1", "kalshi") is None

    def test_persisted_match_is_retrievable(self, db_conn):
        persist_match(
            db_conn, recommendation_id="rec-1", provider="kalshi",
            league="MLB", market_type="game_moneyline",
            match_status="matched", result=_result(),
        )
        stored = get_existing_match(db_conn, "rec-1", "kalshi")
        assert stored is not None
        assert stored["match_status"] == "matched"
        assert stored["confidence"] == 0.99
        assert stored["provider_market_id"] == "M1"

    def test_no_candidates_rejection_persists_without_a_result(self, db_conn):
        persist_match(
            db_conn, recommendation_id="rec-1", provider="kalshi",
            league="MLB", market_type="game_moneyline",
            match_status="no_candidates", result=None,
        )
        stored = get_existing_match(db_conn, "rec-1", "kalshi")
        assert stored["match_status"] == "no_candidates"
        assert stored["confidence"] == 0.0

    def test_rejected_low_confidence_persists_the_score_breakdown(self, db_conn):
        persist_match(
            db_conn, recommendation_id="rec-1", provider="kalshi",
            league="MLB", market_type="game_moneyline",
            match_status="rejected_low_confidence", result=_result(confidence=0.75),
        )
        stored = get_existing_match(db_conn, "rec-1", "kalshi")
        assert stored["match_status"] == "rejected_low_confidence"
        assert stored["confidence"] == 0.75

    def test_persisting_twice_updates_the_same_row_not_a_duplicate(self, db_conn):
        persist_match(
            db_conn, recommendation_id="rec-1", provider="kalshi",
            league="MLB", market_type="game_moneyline",
            match_status="rejected_low_confidence", result=_result(confidence=0.5),
        )
        persist_match(
            db_conn, recommendation_id="rec-1", provider="kalshi",
            league="MLB", market_type="game_moneyline",
            match_status="matched", result=_result(confidence=0.99),
        )
        row = db_conn.execute(
            "SELECT COUNT(*) as n FROM market_matches WHERE recommendation_id = ? AND provider = ?",
            ("rec-1", "kalshi"),
        ).fetchone()
        assert row["n"] == 1
        assert get_existing_match(db_conn, "rec-1", "kalshi")["match_status"] == "matched"

    def test_scoped_by_provider(self, db_conn):
        persist_match(
            db_conn, recommendation_id="rec-1", provider="kalshi",
            league="MLB", market_type="game_moneyline",
            match_status="matched", result=_result(),
        )
        assert get_existing_match(db_conn, "rec-1", "polymarket_us") is None

    def test_scoped_by_matcher_version(self, db_conn):
        persist_match(
            db_conn, recommendation_id="rec-1", provider="kalshi",
            league="MLB", market_type="game_moneyline",
            match_status="matched", result=_result(), matcher_version="v1",
        )
        assert get_existing_match(db_conn, "rec-1", "kalshi", matcher_version="v2") is None
