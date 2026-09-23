"""Tests for the Customer Auto-Bet claim-before-execute mechanism
(database/db_manager.py) -- the same atomic claim pattern already
proven for EV-pick Discord delivery, scoped per (account_id,
recommendation_id, platform) so two concurrent worker passes, or a
worker restart mid-scan, can never both execute the same recommendation
for the same customer ON THE SAME PLATFORM -- while still allowing the
same recommendation to execute independently on Kalshi AND Polymarket
for a customer who has both enabled (2026-09-23 multi-platform
support)."""

from database.db_manager import (
    claim_autobet_recommendation, release_autobet_claim, is_autobet_recommendation_claimed,
    save_autobet_execution, get_autobet_executions, has_executed_autobet_for_recommendation,
)


class TestClaimAutobetRecommendation:
    def test_first_claim_succeeds(self, db_conn):
        assert claim_autobet_recommendation(db_conn, "acct-1", "rec-1", "polymarket_us") is True

    def test_second_claim_for_the_same_triple_fails(self, db_conn):
        claim_autobet_recommendation(db_conn, "acct-1", "rec-1", "polymarket_us")
        assert claim_autobet_recommendation(db_conn, "acct-1", "rec-1", "polymarket_us") is False

    def test_same_recommendation_different_account_both_succeed(self, db_conn):
        """The core user-isolation guarantee at the claim layer: two
        different customers can each independently claim the SAME
        recommendation_id."""
        assert claim_autobet_recommendation(db_conn, "acct-A", "rec-1", "polymarket_us") is True
        assert claim_autobet_recommendation(db_conn, "acct-B", "rec-1", "polymarket_us") is True

    def test_same_recommendation_different_platform_both_succeed(self, db_conn):
        """The core multi-platform guarantee: the SAME customer can
        independently claim the SAME recommendation for Kalshi and for
        Polymarket -- one official pick may legitimately execute once
        on each platform."""
        assert claim_autobet_recommendation(db_conn, "acct-1", "rec-1", "kalshi") is True
        assert claim_autobet_recommendation(db_conn, "acct-1", "rec-1", "polymarket_us") is True

    def test_second_claim_for_the_same_account_rec_and_platform_still_fails(self, db_conn):
        claim_autobet_recommendation(db_conn, "acct-1", "rec-1", "kalshi")
        claim_autobet_recommendation(db_conn, "acct-1", "rec-1", "polymarket_us")
        assert claim_autobet_recommendation(db_conn, "acct-1", "rec-1", "kalshi") is False
        assert claim_autobet_recommendation(db_conn, "acct-1", "rec-1", "polymarket_us") is False

    def test_release_then_reclaim_succeeds(self, db_conn):
        claim_autobet_recommendation(db_conn, "acct-1", "rec-1", "polymarket_us")
        release_autobet_claim(db_conn, "acct-1", "rec-1", "polymarket_us")
        assert claim_autobet_recommendation(db_conn, "acct-1", "rec-1", "polymarket_us") is True

    def test_releasing_one_platform_does_not_release_the_other(self, db_conn):
        claim_autobet_recommendation(db_conn, "acct-1", "rec-1", "kalshi")
        claim_autobet_recommendation(db_conn, "acct-1", "rec-1", "polymarket_us")
        release_autobet_claim(db_conn, "acct-1", "rec-1", "kalshi")
        assert is_autobet_recommendation_claimed(db_conn, "acct-1", "rec-1", "kalshi") is False
        assert is_autobet_recommendation_claimed(db_conn, "acct-1", "rec-1", "polymarket_us") is True

    def test_is_claimed_reflects_current_state(self, db_conn):
        assert is_autobet_recommendation_claimed(db_conn, "acct-1", "rec-1", "polymarket_us") is False
        claim_autobet_recommendation(db_conn, "acct-1", "rec-1", "polymarket_us")
        assert is_autobet_recommendation_claimed(db_conn, "acct-1", "rec-1", "polymarket_us") is True
        release_autobet_claim(db_conn, "acct-1", "rec-1", "polymarket_us")
        assert is_autobet_recommendation_claimed(db_conn, "acct-1", "rec-1", "polymarket_us") is False

    def test_release_of_never_claimed_triple_is_a_safe_noop(self, db_conn):
        release_autobet_claim(db_conn, "acct-1", "rec-never-claimed", "kalshi")  # must not raise


class TestSaveAndReadAutobetExecutions:
    def _execution(self, **overrides):
        base = {
            "account_id": "acct-1", "recommendation_id": "rec-1", "market_type": "game_total_ou",
            "matchup": "Away @ Home", "side": "OVER", "model_ev_pct": 3.5,
            "price_at_detection": 0.52, "price_at_execution": 0.53, "stake_usd": 10.0,
            "filled_quantity": 19.0, "avg_fill_price": 0.53, "status": "EXECUTED",
            "mode": "PAPER", "approval_mode": "AUTO", "platform": "polymarket_us",
        }
        base.update(overrides)
        return base

    def test_save_then_read_round_trips(self, db_conn):
        save_autobet_execution(db_conn, self._execution())
        rows = get_autobet_executions(db_conn, "acct-1")
        assert len(rows) == 1
        assert rows[0]["status"] == "EXECUTED"
        assert rows[0]["stake_usd"] == 10.0
        assert rows[0]["platform"] == "polymarket_us"

    def test_skipped_and_failed_rows_are_saved_too_not_only_successes(self, db_conn):
        save_autobet_execution(db_conn, self._execution(
            status="SKIPPED", skip_reason="EV_BELOW_THRESHOLD", stake_usd=None,
        ))
        rows = get_autobet_executions(db_conn, "acct-1")
        assert rows[0]["status"] == "SKIPPED"
        assert rows[0]["skip_reason"] == "EV_BELOW_THRESHOLD"

    def test_ordered_newest_first(self, db_conn):
        import time
        save_autobet_execution(db_conn, self._execution(recommendation_id="rec-old"))
        time.sleep(0.01)
        save_autobet_execution(db_conn, self._execution(recommendation_id="rec-new"))
        rows = get_autobet_executions(db_conn, "acct-1")
        assert rows[0]["recommendation_id"] == "rec-new"

    def test_limit_is_respected(self, db_conn):
        for i in range(5):
            save_autobet_execution(db_conn, self._execution(recommendation_id=f"rec-{i}"))
        rows = get_autobet_executions(db_conn, "acct-1", limit=2)
        assert len(rows) == 2

    def test_only_returns_this_accounts_rows(self, db_conn):
        save_autobet_execution(db_conn, self._execution(account_id="acct-A"))
        save_autobet_execution(db_conn, self._execution(account_id="acct-B"))
        rows_a = get_autobet_executions(db_conn, "acct-A")
        assert len(rows_a) == 1
        assert rows_a[0]["account_id"] == "acct-A"

    def test_platform_filter_returns_only_that_platform(self, db_conn):
        save_autobet_execution(db_conn, self._execution(recommendation_id="rec-k", platform="kalshi"))
        save_autobet_execution(db_conn, self._execution(recommendation_id="rec-p", platform="polymarket_us"))
        kalshi_rows = get_autobet_executions(db_conn, "acct-1", platform="kalshi")
        assert len(kalshi_rows) == 1
        assert kalshi_rows[0]["recommendation_id"] == "rec-k"

    def test_polymarket_filter_includes_legacy_null_platform_rows(self, db_conn):
        """Historical rows written before multi-platform support have
        platform=NULL and were always Polymarket -- the polymarket_us
        filter must still surface them for backward compatibility."""
        row = self._execution(recommendation_id="rec-legacy")
        del row["platform"]
        save_autobet_execution(db_conn, row)
        save_autobet_execution(db_conn, self._execution(recommendation_id="rec-k", platform="kalshi"))
        polymarket_rows = get_autobet_executions(db_conn, "acct-1", platform="polymarket_us")
        ids = {r["recommendation_id"] for r in polymarket_rows}
        assert ids == {"rec-legacy"}

    def test_no_filter_returns_every_platform(self, db_conn):
        save_autobet_execution(db_conn, self._execution(recommendation_id="rec-k", platform="kalshi"))
        save_autobet_execution(db_conn, self._execution(recommendation_id="rec-p", platform="polymarket_us"))
        rows = get_autobet_executions(db_conn, "acct-1")
        assert len(rows) == 2


class TestHasExecutedAutobetForRecommendation:
    def _execution(self, **overrides):
        base = {
            "account_id": "acct-1", "recommendation_id": "rec-1", "status": "EXECUTED", "mode": "PAPER",
            "platform": "polymarket_us",
        }
        base.update(overrides)
        return base

    def test_false_when_nothing_saved(self, db_conn):
        assert has_executed_autobet_for_recommendation(db_conn, "acct-1", "rec-1", "polymarket_us") is False

    def test_true_for_executed(self, db_conn):
        save_autobet_execution(db_conn, self._execution(status="EXECUTED"))
        assert has_executed_autobet_for_recommendation(db_conn, "acct-1", "rec-1", "polymarket_us") is True

    def test_true_for_partially_filled(self, db_conn):
        save_autobet_execution(db_conn, self._execution(status="PARTIALLY_FILLED"))
        assert has_executed_autobet_for_recommendation(db_conn, "acct-1", "rec-1", "polymarket_us") is True

    def test_false_for_skipped(self, db_conn):
        save_autobet_execution(db_conn, self._execution(status="SKIPPED"))
        assert has_executed_autobet_for_recommendation(db_conn, "acct-1", "rec-1", "polymarket_us") is False

    def test_false_for_failed(self, db_conn):
        save_autobet_execution(db_conn, self._execution(status="FAILED"))
        assert has_executed_autobet_for_recommendation(db_conn, "acct-1", "rec-1", "polymarket_us") is False

    def test_scoped_per_account_not_global(self, db_conn):
        save_autobet_execution(db_conn, self._execution(account_id="acct-A", status="EXECUTED"))
        assert has_executed_autobet_for_recommendation(db_conn, "acct-B", "rec-1", "polymarket_us") is False

    def test_scoped_per_platform_kalshi_execution_does_not_block_polymarket(self, db_conn):
        """The multi-platform guarantee at the duplicate-check layer:
        an EXECUTED Kalshi row must not make has_executed_... return
        True for the polymarket_us platform on the same recommendation."""
        save_autobet_execution(db_conn, self._execution(status="EXECUTED", platform="kalshi"))
        assert has_executed_autobet_for_recommendation(db_conn, "acct-1", "rec-1", "kalshi") is True
        assert has_executed_autobet_for_recommendation(db_conn, "acct-1", "rec-1", "polymarket_us") is False

    def test_polymarket_check_includes_legacy_null_platform_rows(self, db_conn):
        row = self._execution(status="EXECUTED")
        del row["platform"]
        save_autobet_execution(db_conn, row)
        assert has_executed_autobet_for_recommendation(db_conn, "acct-1", "rec-1", "polymarket_us") is True
