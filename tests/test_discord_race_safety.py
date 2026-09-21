"""Tests for the 2026-09-18 Discord delivery audit/hardening:

- claim-before-send for EV picks (database.db_manager.claim_recommendations_
  for_alert/release_recommendation_alerts), replacing the old send-then-mark
  order in src.discord_delivery.deliver_new_recommendation_alerts.
- claim-before-send for arbitrage/middle opportunities, folded directly into
  sync_arbitrage_opportunities/sync_middle_opportunities's "new_ids" so a
  caller never needs (and can't race on) its own separate claim step.
- src.worker._deliver_new_opportunity_alerts releasing a claim when the
  actual Discord send fails, so a failure is retried, not stuck forever.

All Discord network calls are mocked -- no real webhook traffic.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

from database.db_manager import (
    claim_recommendations_for_alert, release_recommendation_alerts,
    get_unalerted_recommendation_ids,
    claim_arbitrage_for_discord, release_arbitrage_discord_claim,
    claim_middle_for_discord, release_middle_discord_claim,
    sync_arbitrage_opportunities, sync_middle_opportunities,
    init_db,
)
from src import worker


def _arb_opp(group_key="E1|P1|k|6.5", roi=8.5):
    return {
        "group_key": group_key, "event_id": "E1", "matchup": "Away @ Home",
        "event_start_time": "2026-09-09T20:00:00+00:00",
        "player_id": "P1", "player_name": "Test Pitcher",
        "market_type": "pitching_strikeouts_ou", "line": 6.5,
        "side_a": "OVER", "side_a_book": "BookA", "side_a_price": 110,
        "side_a_decimal_odds": 2.10, "side_a_stake_pct": 0.523,
        "side_b": "UNDER", "side_b_book": "BookB", "side_b_price": 130,
        "side_b_decimal_odds": 2.30, "side_b_stake_pct": 0.477,
        "guaranteed_roi_pct": roi,
    }


def _mid_opp():
    return {
        "event_id": "E1", "matchup": "Away @ Home",
        "event_start_time": "2026-09-09T20:00:00+00:00",
        "player_id": "P1", "player_name": "Test Batter",
        "market_type": "batting_totalBases_ou",
        "over_line": 1.5, "over_sportsbook": "BookA", "over_price": -110,
        "over_decimal_odds": 1.909, "over_stake_pct": 0.5,
        "under_line": 2.5, "under_sportsbook": "BookB", "under_price": -110,
        "under_decimal_odds": 1.909, "under_stake_pct": 0.5,
        "window_width": 1.0, "worst_case_roi_pct": -2.0, "best_case_roi_pct": 90.0,
        "verdict": "WORTH_IT",
    }


class TestEvRecommendationClaim:
    def test_claim_succeeds_for_unclaimed_ids(self, db_conn):
        claimed = claim_recommendations_for_alert(db_conn, ["rec-1", "rec-2"])
        assert claimed == ["rec-1", "rec-2"]

    def test_second_claim_of_the_same_ids_gets_nothing(self, db_conn):
        """Simulates two processes racing to claim the same picks -- only
        the first should win either one."""
        first = claim_recommendations_for_alert(db_conn, ["rec-1", "rec-2"])
        second = claim_recommendations_for_alert(db_conn, ["rec-1", "rec-2"])
        assert first == ["rec-1", "rec-2"]
        assert second == []

    def test_partial_overlap_only_claims_the_unclaimed_ones(self, db_conn):
        claim_recommendations_for_alert(db_conn, ["rec-1"])
        second = claim_recommendations_for_alert(db_conn, ["rec-1", "rec-2"])
        assert second == ["rec-2"]

    def test_release_allows_reclaiming(self, db_conn):
        claim_recommendations_for_alert(db_conn, ["rec-1"])
        release_recommendation_alerts(db_conn, ["rec-1"])
        reclaimed = claim_recommendations_for_alert(db_conn, ["rec-1"])
        assert reclaimed == ["rec-1"]

    def test_claimed_ids_are_unalerted_ids_excluded(self, db_conn):
        """claim_recommendations_for_alert and get_unalerted_recommendation_ids
        share the same discord_alerts_sent table -- a claim must be
        visible to the dedup check other callers use."""
        claim_recommendations_for_alert(db_conn, ["rec-1"])
        remaining = get_unalerted_recommendation_ids(db_conn, ["rec-1", "rec-2"])
        assert remaining == ["rec-2"]

    def test_empty_input_is_a_no_op(self, db_conn):
        assert claim_recommendations_for_alert(db_conn, []) == []
        release_recommendation_alerts(db_conn, [])  # must not raise


class TestArbitrageAndMiddleOpportunityClaim:
    def test_claim_arbitrage_succeeds_once(self, db_conn):
        sync_arbitrage_opportunities(db_conn, "MLB", [_arb_opp()])
        # The sync itself already claimed it (new_ids), so a second,
        # independent claim attempt for the same id must find nothing.
        second = claim_arbitrage_for_discord(db_conn, ["E1|P1|k|6.5"])
        assert second == []

    def test_claim_middle_succeeds_once(self, db_conn):
        sync_middle_opportunities(db_conn, "MLB", [_mid_opp()])
        second = claim_middle_for_discord(db_conn, ["E1|P1|batting_totalBases_ou|1.5|2.5"])
        assert second == []

    def test_release_arbitrage_claim_allows_reclaim(self, db_conn):
        result = sync_arbitrage_opportunities(db_conn, "MLB", [_arb_opp()])
        opp_id = result["new_ids"][0]
        release_arbitrage_discord_claim(db_conn, [opp_id])
        reclaimed = claim_arbitrage_for_discord(db_conn, [opp_id])
        assert reclaimed == [opp_id]

    def test_release_middle_claim_allows_reclaim(self, db_conn):
        result = sync_middle_opportunities(db_conn, "MLB", [_mid_opp()])
        opp_id = result["new_ids"][0]
        release_middle_discord_claim(db_conn, [opp_id])
        reclaimed = claim_middle_for_discord(db_conn, [opp_id])
        assert reclaimed == [opp_id]

    def test_two_concurrent_syncs_of_the_same_new_opportunity_only_one_claims_it(self, db_conn):
        """Simulates the race the user explicitly asked to be protected
        against: two workers/processes both syncing the same league at
        once. Both would see the opportunity as newly ACTIVE, but the
        atomic claim inside sync_* must let only one of them walk away
        with it in new_ids."""
        opp = _arb_opp()
        first = sync_arbitrage_opportunities(db_conn, "MLB", [opp])
        second = sync_arbitrage_opportunities(db_conn, "MLB", [opp])
        assert first["new_ids"] == ["E1|P1|k|6.5"]
        assert second["new_ids"] == []

    def test_invalid_table_name_is_rejected(self, db_conn):
        import pytest
        from database.db_manager import _claim_opportunity_for_discord
        with pytest.raises(ValueError):
            _claim_opportunity_for_discord(db_conn, "historical_recommendations", ["x"])


class TestWorkerReleasesClaimOnDeliveryFailure:
    def test_failed_arbitrage_send_releases_the_claim_for_retry(self, db_conn):
        from database.db_manager import get_active_arbitrage_opportunities

        class FakeConfig:
            discord_webhook_urls = ""
            discord_webhook_urls_arb_middle = "https://discord.com/api/webhooks/arb-mid"
            discord_webhook_urls_middle = ""
            database_path = "unused"
            min_confidence_score = 40.0
            min_ev_pct = 2.0

        sync_result = sync_arbitrage_opportunities(db_conn, "MLB", [_arb_opp()])
        opp_id = sync_result["new_ids"][0]
        opp = _arb_opp()
        opp["opportunity_id"] = opp_id

        def fake_run_scan(conn, league="MLB", **kwargs):
            return {"league": league, "rows_examined": 0,
                    "arbitrage": {"detected": 1}, "middles": {"detected": 0},
                    "new_arbitrage": [opp] if league == "MLB" else [],
                    "new_middles": []}

        with patch("src.arb_middle_scan.run_scan", side_effect=fake_run_scan), \
             patch("src.discord_delivery.deliver_arbitrage_alerts", return_value={"sent": 0, "errors": 1}):
            worker._run_arb_middle_scan(db_conn, config=FakeConfig())

        row = [r for r in get_active_arbitrage_opportunities(db_conn, "MLB") if r["opportunity_id"] == opp_id][0]
        assert row["discord_sent"] == 0

        # And it's claimable again -- a future scan could retry it.
        reclaimed = claim_arbitrage_for_discord(db_conn, [opp_id])
        assert reclaimed == [opp_id]

    def test_arbitrage_send_exception_also_releases_the_claim(self, db_conn):
        """A raised exception (network error, bad webhook, etc.), not
        just a soft {"errors": N>0} result, must also release the claim
        -- otherwise an opportunity that hit a real exception would be
        permanently stuck looking "already sent" with nothing ever
        delivered."""
        from database.db_manager import get_active_arbitrage_opportunities

        class FakeConfig:
            discord_webhook_urls = ""
            discord_webhook_urls_arb_middle = "https://discord.com/api/webhooks/arb-mid"
            discord_webhook_urls_middle = ""
            database_path = "unused"
            min_confidence_score = 40.0
            min_ev_pct = 2.0

        sync_result = sync_arbitrage_opportunities(db_conn, "MLB", [_arb_opp()])
        opp_id = sync_result["new_ids"][0]
        opp = _arb_opp()
        opp["opportunity_id"] = opp_id

        def fake_run_scan(conn, league="MLB", **kwargs):
            return {"league": league, "rows_examined": 0,
                    "arbitrage": {"detected": 1}, "middles": {"detected": 0},
                    "new_arbitrage": [opp] if league == "MLB" else [],
                    "new_middles": []}

        with patch("src.arb_middle_scan.run_scan", side_effect=fake_run_scan), \
             patch("src.discord_delivery.deliver_arbitrage_alerts", side_effect=RuntimeError("boom")):
            result = worker._run_arb_middle_scan(db_conn, config=FakeConfig())

        assert result["status"] == "success"  # never crashes the scan job
        row = [r for r in get_active_arbitrage_opportunities(db_conn, "MLB") if r["opportunity_id"] == opp_id][0]
        assert row["discord_sent"] == 0

    def test_failed_middle_send_releases_the_claim_for_retry(self, db_conn):
        from database.db_manager import get_active_middle_opportunities

        class FakeConfig:
            discord_webhook_urls = ""
            discord_webhook_urls_arb_middle = ""
            discord_webhook_urls_middle = "https://discord.com/api/webhooks/middle-only"
            database_path = "unused"
            min_confidence_score = 40.0
            min_ev_pct = 2.0

        sync_result = sync_middle_opportunities(db_conn, "MLB", [_mid_opp()])
        opp_id = sync_result["new_ids"][0]
        opp = _mid_opp()
        opp["opportunity_id"] = opp_id

        def fake_run_scan(conn, league="MLB", **kwargs):
            return {"league": league, "rows_examined": 0,
                    "arbitrage": {"detected": 0}, "middles": {"detected": 1},
                    "new_arbitrage": [],
                    "new_middles": [opp] if league == "MLB" else []}

        with patch("src.arb_middle_scan.run_scan", side_effect=fake_run_scan), \
             patch("src.discord_delivery.deliver_middle_alerts", return_value={"sent": 0, "errors": 1}):
            worker._run_arb_middle_scan(db_conn, config=FakeConfig())

        row = [r for r in get_active_middle_opportunities(db_conn, "MLB") if r["opportunity_id"] == opp_id][0]
        assert row["discord_sent"] == 0
        assert claim_middle_for_discord(db_conn, [opp_id]) == [opp_id]

    def test_successful_middle_send_leaves_the_claim_in_place(self, db_conn):
        from database.db_manager import get_active_middle_opportunities

        class FakeConfig:
            discord_webhook_urls = ""
            discord_webhook_urls_arb_middle = ""
            discord_webhook_urls_middle = "https://discord.com/api/webhooks/middle-only"
            database_path = "unused"
            min_confidence_score = 40.0
            min_ev_pct = 2.0

        sync_result = sync_middle_opportunities(db_conn, "MLB", [_mid_opp()])
        opp_id = sync_result["new_ids"][0]
        opp = _mid_opp()
        opp["opportunity_id"] = opp_id

        def fake_run_scan(conn, league="MLB", **kwargs):
            return {"league": league, "rows_examined": 0,
                    "arbitrage": {"detected": 0}, "middles": {"detected": 1},
                    "new_arbitrage": [],
                    "new_middles": [opp] if league == "MLB" else []}

        with patch("src.arb_middle_scan.run_scan", side_effect=fake_run_scan), \
             patch("src.discord_delivery.deliver_middle_alerts", return_value={"sent": 1, "errors": 0}):
            worker._run_arb_middle_scan(db_conn, config=FakeConfig())

        row = [r for r in get_active_middle_opportunities(db_conn, "MLB") if r["opportunity_id"] == opp_id][0]
        assert row["discord_sent"] == 1
        # Not reclaimable -- it's genuinely already sent.
        assert claim_middle_for_discord(db_conn, [opp_id]) == []


class TestScannerRunsTwiceEndToEnd:
    """The literal "scanner runs twice -> only one Discord post" case
    from real detection through to the Discord webhook call, for both
    EV picks (via deliver_new_recommendation_alerts) and middles (via
    the full sync -> new_ids -> deliver_middle_alerts pipeline)."""

    def test_middle_scan_run_twice_posts_once(self, db_conn):
        opp = _mid_opp()

        with patch("src.arb_middle_scan.find_middle_opportunities", return_value=[dict(opp)]), \
             patch("src.arb_middle_scan.find_arbitrage_opportunities", return_value=[]), \
             patch("src.arb_middle_scan._fetch_recent_odds_rows", return_value=[{"event_id": "E1"}]), \
             patch("src.discord_delivery._send_webhook_raw", return_value=True) as mock_send:
            from src.arb_middle_scan import run_scan
            first = run_scan(db_conn, league="MLB")
            second = run_scan(db_conn, league="MLB")

        assert len(first["new_middles"]) == 1
        assert len(second["new_middles"]) == 0
        # run_scan itself never calls Discord (src/worker.py does) --
        # this just confirms the dedup signal a caller would act on.
        assert not mock_send.called

    def test_ev_pick_scan_run_twice_via_deliver_posts_once(self, tmp_path: Path):
        from src.discord_delivery import deliver_new_recommendation_alerts

        db_path = tmp_path / "full.db"
        init_db(str(db_path))
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            INSERT INTO historical_recommendations (
                recommendation_id, fingerprint, event_id, player_id, player_name,
                market_type, market_form, period, line, side, sportsbook,
                offered_american_odds, offered_decimal_odds, offered_implied_prob,
                ev_pct, rec_status, rec_eligible, scan_timestamp
            ) VALUES (
                'rec-1', 'fp-1', 'E1', 'P1', 'Judge',
                'strikeouts', 'ou', 'full_game', 6.5, 'OVER', 'DK',
                -110, 1.909, 0.524,
                5.0, 'STRONG_EDGE', 1, '2026-09-10T00:00:00+00:00'
            )
        """)
        conn.commit()
        conn.close()

        with patch("src.discord_delivery._send_webhook_raw", return_value=True) as mock_send:
            first = deliver_new_recommendation_alerts(
                db_path, ["https://discord.com/api/webhooks/test"], min_confidence=0, min_ev_pct=0,
            )
            second = deliver_new_recommendation_alerts(
                db_path, ["https://discord.com/api/webhooks/test"], min_confidence=0, min_ev_pct=0,
            )

        assert first["sent"] == 1
        assert second["sent"] == 0
        assert mock_send.call_count == 1


class TestDryRunDoesNotLeaveAPermanentClaim:
    def test_dry_run_releases_its_claim(self, tmp_path: Path):
        from src.discord_delivery import deliver_new_recommendation_alerts

        db_path = tmp_path / "full.db"
        init_db(str(db_path))
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            INSERT INTO historical_recommendations (
                recommendation_id, fingerprint, event_id, player_id, player_name,
                market_type, market_form, period, line, side, sportsbook,
                offered_american_odds, offered_decimal_odds, offered_implied_prob,
                ev_pct, rec_status, rec_eligible, scan_timestamp
            ) VALUES (
                'rec-1', 'fp-1', 'E1', 'P1', 'Judge',
                'strikeouts', 'ou', 'full_game', 6.5, 'OVER', 'DK',
                -110, 1.909, 0.524,
                5.0, 'STRONG_EDGE', 1, '2026-09-10T00:00:00+00:00'
            )
        """)
        conn.commit()
        conn.close()

        dry = deliver_new_recommendation_alerts(
            db_path, ["https://discord.com/api/webhooks/test"],
            min_confidence=0, min_ev_pct=0, dry_run=True,
        )
        assert dry["sent"] == 1

        with patch("src.discord_delivery._send_webhook_raw", return_value=True) as mock_send:
            real = deliver_new_recommendation_alerts(
                db_path, ["https://discord.com/api/webhooks/test"], min_confidence=0, min_ev_pct=0,
            )
        assert real["sent"] == 1
        assert mock_send.called


class TestMessageFormatterReceivesCorrectData:
    def test_new_ev_alert_payload_contains_the_real_recommendation_fields(self, tmp_path: Path):
        """End-to-end: deliver_new_recommendation_alerts -> format_recommendation
        -> the actual Discord payload, confirming matchup/league/fair_prob/
        implied_prob actually reach the message, not just that SOME text
        gets sent."""
        from src.discord_delivery import deliver_new_recommendation_alerts

        db_path = tmp_path / "full.db"
        init_db(str(db_path))
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            INSERT INTO historical_recommendations (
                recommendation_id, fingerprint, event_id, player_id, player_name,
                market_type, market_form, period, line, side, sportsbook,
                offered_american_odds, offered_decimal_odds, offered_implied_prob,
                fair_prob, n_consensus_books, ev_pct, rec_status, rec_eligible,
                scan_timestamp, matchup, league
            ) VALUES (
                'rec-1', 'fp-1', 'E1', 'P1', 'Judge',
                'strikeouts', 'ou', 'full_game', 6.5, 'OVER', 'DK',
                -110, 1.909, 0.524,
                0.55, 7, 5.0, 'STRONG_EDGE', 1,
                '2026-09-10T00:00:00+00:00', 'Yankees @ Red Sox', 'MLB'
            )
        """)
        conn.commit()
        conn.close()

        with patch("src.discord_delivery._send_webhook_raw", return_value=True) as mock_send:
            deliver_new_recommendation_alerts(
                db_path, ["https://discord.com/api/webhooks/test"], min_confidence=0, min_ev_pct=0,
            )

        payload = mock_send.call_args[0][1]
        content = payload["content"]
        assert "Judge" in content
        assert "Yankees @ Red Sox" in content
        assert "MLB" in content
        assert "55.0" in content  # fair_prob
        assert "52.4" in content  # offered_implied_prob
        # n_consensus_books is no longer shown in the message (removed
        # 2026-09-21 per direct operator feedback) -- not asserted here.


class TestUnconfiguredChannelDoesNotPermanentlyBurnAClaim:
    """2026-09-18 (found via the `simulate` CLI command against a real
    local mock server): sync_arbitrage_opportunities/sync_middle_
    opportunities claim every newly-ACTIVE opportunity UNCONDITIONALLY,
    regardless of whether any webhook is configured (see their
    docstrings -- the claim is what "new_ids" now IS). Left unfixed,
    this meant a middle/arbitrage opportunity detected while that
    channel had no webhook configured would be marked discord_sent=1
    forever despite nothing ever being sent -- so it would NEVER alert
    even after an operator configured that webhook later. Fixed by
    src.worker._release_undeliverable_claims, called unconditionally
    from _run_arb_middle_scan before the delivery step."""

    def test_middle_claim_is_released_when_middle_webhook_not_configured(self, db_conn):
        from database.db_manager import get_active_middle_opportunities

        class FakeConfig:
            discord_webhook_urls = ""
            discord_webhook_urls_arb_middle = ""
            discord_webhook_urls_middle = ""  # fully disabled
            database_path = "unused"
            min_confidence_score = 40.0
            min_ev_pct = 2.0

        opp = _mid_opp()

        def fake_run_scan(conn, league="MLB", **kwargs):
            if league != "MLB":
                return {"league": league, "rows_examined": 0,
                        "arbitrage": {"detected": 0}, "middles": {"detected": 0},
                        "new_arbitrage": [], "new_middles": []}
            sync_result = sync_middle_opportunities(conn, "MLB", [dict(opp)])
            return {"league": league, "rows_examined": 0,
                    "arbitrage": {"detected": 0}, "middles": {"detected": 1, **sync_result},
                    "new_arbitrage": [],
                    "new_middles": [o for o in [dict(opp, opportunity_id=sync_result["new_ids"][0])]
                                    if sync_result["new_ids"]]}

        with patch("src.arb_middle_scan.run_scan", side_effect=fake_run_scan):
            worker._run_arb_middle_scan(db_conn, config=FakeConfig())

        active = get_active_middle_opportunities(db_conn, "MLB")
        row = [r for r in active if r["opportunity_id"] == "E1|P1|batting_totalBases_ou|1.5|2.5"][0]
        assert row["discord_sent"] == 0, (
            "a middle detected while the channel is unconfigured must stay reclaimable, "
            "not permanently marked as sent"
        )

    def test_reclaimed_and_delivered_once_the_webhook_is_later_configured(self, db_conn):
        """The real-world sequence: Discord fully off for a while (the
        opportunity stays active and unconfigured-but-claimed-then-
        released across several scans), then the operator configures
        the middle webhook -- the very next scan must still alert it,
        not skip it as "already sent"."""
        from database.db_manager import get_active_middle_opportunities

        class DisabledConfig:
            discord_webhook_urls = ""
            discord_webhook_urls_arb_middle = ""
            discord_webhook_urls_middle = ""
            database_path = "unused"
            min_confidence_score = 40.0
            min_ev_pct = 2.0

        class EnabledConfig(DisabledConfig):
            discord_webhook_urls_middle = "https://discord.com/api/webhooks/middle-only"

        opp = _mid_opp()

        def fake_run_scan(conn, league="MLB", **kwargs):
            if league != "MLB":
                return {"league": league, "rows_examined": 0,
                        "arbitrage": {"detected": 0}, "middles": {"detected": 0},
                        "new_arbitrage": [], "new_middles": []}
            sync_result = sync_middle_opportunities(conn, "MLB", [dict(opp)])
            new_ids = set(sync_result["new_ids"])
            return {"league": league, "rows_examined": 0,
                    "arbitrage": {"detected": 0}, "middles": {"detected": 1, **sync_result},
                    "new_arbitrage": [],
                    "new_middles": [dict(opp, opportunity_id=i) for i in new_ids]}

        with patch("src.arb_middle_scan.run_scan", side_effect=fake_run_scan):
            # Two scans while disabled -- must never get permanently claimed.
            worker._run_arb_middle_scan(db_conn, config=DisabledConfig())
            worker._run_arb_middle_scan(db_conn, config=DisabledConfig())

            # Now the operator configures the middle webhook.
            with patch("src.discord_delivery.deliver_middle_alerts",
                       return_value={"sent": 1, "errors": 0}) as mock_deliver:
                worker._run_arb_middle_scan(db_conn, config=EnabledConfig())

        mock_deliver.assert_called_once()
        delivered = mock_deliver.call_args[0][0]
        assert len(delivered) == 1
        active = get_active_middle_opportunities(db_conn, "MLB")
        row = [r for r in active if r["opportunity_id"] == "E1|P1|batting_totalBases_ou|1.5|2.5"][0]
        assert row["discord_sent"] == 1

    def test_arbitrage_claim_released_when_only_middle_channel_is_configured(self, db_conn):
        """Partial configuration: middle webhook set, arbitrage not --
        the arbitrage claim run_scan already made must still be
        released, not silently kept just because SOME channel is
        configured for this scan."""
        from database.db_manager import get_active_arbitrage_opportunities

        class FakeConfig:
            discord_webhook_urls = ""
            discord_webhook_urls_arb_middle = ""  # arbitrage NOT configured
            discord_webhook_urls_middle = "https://discord.com/api/webhooks/middle-only"
            database_path = "unused"
            min_confidence_score = 40.0
            min_ev_pct = 2.0

        arb = _arb_opp()

        def fake_run_scan(conn, league="MLB", **kwargs):
            if league != "MLB":
                return {"league": league, "rows_examined": 0,
                        "arbitrage": {"detected": 0}, "middles": {"detected": 0},
                        "new_arbitrage": [], "new_middles": []}
            arb_sync = sync_arbitrage_opportunities(conn, "MLB", [dict(arb)])
            return {"league": league, "rows_examined": 0,
                    "arbitrage": {"detected": 1, **arb_sync}, "middles": {"detected": 0},
                    "new_arbitrage": [dict(arb, opportunity_id=i) for i in arb_sync["new_ids"]],
                    "new_middles": []}

        with patch("src.arb_middle_scan.run_scan", side_effect=fake_run_scan), \
             patch("src.discord_delivery.deliver_middle_alerts", return_value={"sent": 0, "errors": 0}):
            worker._run_arb_middle_scan(db_conn, config=FakeConfig())

        active = get_active_arbitrage_opportunities(db_conn, "MLB")
        row = [r for r in active if r["opportunity_id"] == "E1|P1|k|6.5"][0]
        assert row["discord_sent"] == 0

    def test_release_undeliverable_claims_directly(self, db_conn):
        from src.worker import _release_undeliverable_claims
        from database.db_manager import get_active_middle_opportunities, get_active_arbitrage_opportunities

        sync_arbitrage_opportunities(db_conn, "MLB", [_arb_opp()])
        sync_middle_opportunities(db_conn, "MLB", [_mid_opp()])

        class NothingConfigured:
            discord_webhook_urls_arb_middle = ""
            discord_webhook_urls_middle = ""

        results = {
            "MLB": {
                "new_arbitrage": [{"opportunity_id": "E1|P1|k|6.5"}],
                "new_middles": [{"opportunity_id": "E1|P1|batting_totalBases_ou|1.5|2.5"}],
            }
        }
        _release_undeliverable_claims(db_conn, NothingConfigured(), results)

        arb_row = get_active_arbitrage_opportunities(db_conn, "MLB")[0]
        mid_row = get_active_middle_opportunities(db_conn, "MLB")[0]
        assert arb_row["discord_sent"] == 0
        assert mid_row["discord_sent"] == 0

    def test_release_undeliverable_claims_leaves_configured_channels_alone(self, db_conn):
        """Only releases a claim for a channel with NO webhook -- must
        never touch discord_sent for a channel that IS configured
        (that's _deliver_new_opportunity_alerts's job, which only
        releases on an actual send failure)."""
        from src.worker import _release_undeliverable_claims
        from database.db_manager import get_active_middle_opportunities

        sync_middle_opportunities(db_conn, "MLB", [_mid_opp()])

        class MiddleConfigured:
            discord_webhook_urls_arb_middle = ""
            discord_webhook_urls_middle = "https://discord.com/api/webhooks/middle-only"

        results = {"MLB": {"new_arbitrage": [], "new_middles": [{"opportunity_id": "E1|P1|batting_totalBases_ou|1.5|2.5"}]}}
        _release_undeliverable_claims(db_conn, MiddleConfigured(), results)

        mid_row = get_active_middle_opportunities(db_conn, "MLB")[0]
        # Still claimed -- this channel IS configured, so its claim is
        # left for _deliver_new_opportunity_alerts to actually use.
        assert mid_row["discord_sent"] == 1
