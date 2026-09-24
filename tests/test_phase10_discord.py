"""Tests for Phase 10 Part D: Discord Delivery."""

from __future__ import annotations

import json
import sqlite3
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _seed_rec(conn, rec_id, *, player="Judge", ev=5.0, adv=None, status="STRONG_EDGE", official=True,
              start_delta_hours=3, pick_status="ACTIVE"):
    """Insert a REAL-schema recommendation (and, if *official*, its ACTIVE
    official_picks row). The Discord loader reads official picks only."""
    from datetime import datetime, timedelta, timezone
    start = (datetime.now(timezone.utc) + timedelta(hours=start_delta_hours)).isoformat()
    conn.execute(
        """INSERT INTO historical_recommendations (
               recommendation_id, fingerprint, event_id, player_id, player_name, market_type, market_form,
               period, line, side, sportsbook, offered_american_odds, offered_decimal_odds,
               offered_implied_prob, ev_pct, yn_implied_prob_adv, rec_status, rec_eligible, scan_timestamp,
               event_start_time)
           VALUES (?, ?, ?, ?, ?, 'strikeouts', 'ou', 'full_game', 6.5, 'OVER', 'DK', -110, 1.909, 0.524,
                   ?, ?, ?, 1, ?, ?)""",
        (rec_id, f"fp-{rec_id}", f"E-{rec_id}", f"P-{rec_id}", player, ev, adv, status,
         datetime.now(timezone.utc).isoformat(), start),
    )
    if official:
        conn.execute(
            "INSERT INTO official_picks (recommendation_id, tier, official_rank, pick_status) "
            "VALUES (?, 'OFFICIAL_TRACKED', 1, ?)", (rec_id, pick_status),
        )
    conn.commit()



class TestDiscordDelivery:

    def _make_db_with_recs(self, tmp_path: Path) -> Path:
        from database.db_manager import init_db
        db_path = tmp_path / "test.db"
        init_db(str(db_path))
        conn = sqlite3.connect(str(db_path))
        _seed_rec(conn, "rec-1", player="Judge", ev=5.0, status="STRONG_EDGE")
        _seed_rec(conn, "rec-2", player="Ohtani", ev=None, adv=6.0, status="POSITIVE_EDGE")
        conn.close()
        return db_path

    def test_deliver_no_webhooks(self, tmp_path):
        from src.discord_delivery import deliver_recommendations
        db_path = self._make_db_with_recs(tmp_path)
        result = deliver_recommendations(db_path, [])
        assert result["sent"] == 0

    def test_deliver_dry_run(self, tmp_path):
        from src.discord_delivery import deliver_recommendations
        db_path = self._make_db_with_recs(tmp_path)
        result = deliver_recommendations(
            db_path, ["https://discord.com/api/webhooks/test"], dry_run=True
        )
        assert result["sent"] >= 1

    def test_deliver_sends_webhook(self, tmp_path):
        from src.discord_delivery import deliver_recommendations
        db_path = self._make_db_with_recs(tmp_path)

        with patch("src.discord_delivery._send_webhook_raw", return_value=True) as mock:
            result = deliver_recommendations(
                db_path, ["https://discord.com/api/webhooks/test"],
                min_confidence=0, min_ev_pct=0,
            )
            assert result["sent"] >= 1
            assert mock.called

    def test_deliver_multiple_webhooks(self, tmp_path):
        from src.discord_delivery import deliver_recommendations
        db_path = self._make_db_with_recs(tmp_path)

        with patch("src.discord_delivery._send_webhook_raw", return_value=True) as mock:
            urls = ["https://discord.com/api/webhooks/1", "https://discord.com/api/webhooks/2"]
            result = deliver_recommendations(
                db_path, urls, min_confidence=0, min_ev_pct=0,
            )
            assert result["sent"] >= 2

    def test_deliver_filters_by_min_ev(self, tmp_path):
        from database.db_manager import init_db
        from src.discord_delivery import deliver_recommendations
        db_path = tmp_path / "low_ev.db"
        init_db(str(db_path))
        conn = sqlite3.connect(str(db_path))
        _seed_rec(conn, "rec-low", ev=0.01, adv=0.01)
        conn.close()
        with patch("src.discord_delivery._send_webhook_raw", return_value=True):
            result = deliver_recommendations(
                db_path, ["https://discord.com/api/webhooks/test"],
                min_confidence=0, min_ev_pct=5.0,
            )
            assert result["sent"] == 0

    def test_send_webhook_message(self):
        from src.discord_delivery import send_webhook_message
        with patch("src.discord_delivery._send_webhook_raw", return_value=True) as mock:
            result = send_webhook_message(
                "https://discord.com/api/webhooks/test",
                "Hello world",
                embed_title="Test",
            )
            assert result is True
            call_args = mock.call_args
            payload = call_args[0][1]
            assert "embeds" in payload
            assert payload["embeds"][0]["title"] == "Test"

    def test_send_webhook_message_no_embed(self):
        from src.discord_delivery import send_webhook_message
        with patch("src.discord_delivery._send_webhook_raw", return_value=True) as mock:
            result = send_webhook_message(
                "https://discord.com/api/webhooks/test", "Hello"
            )
            assert result is True
            payload = mock.call_args[0][1]
            assert payload["content"] == "Hello"

    def test_send_webhook_raw_success(self):
        from src.discord_delivery import _send_webhook_raw
        mock_resp = MagicMock()
        mock_resp.getcode.return_value = 204
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _send_webhook_raw("https://discord.com/api/webhooks/test", {"content": "hi"})
            assert result is True

    def test_send_webhook_raw_http_error(self):
        from src.discord_delivery import _send_webhook_raw
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("fail")):
            result = _send_webhook_raw("https://discord.com/api/webhooks/test", {"content": "hi"})
            assert result is False

    def test_send_webhook_raw_rate_limit_retry(self):
        from src.discord_delivery import _send_webhook_raw
        rate_limited = urllib.error.HTTPError(
            url="test", code=429, msg="rate limited",
            hdrs=None, fp=MagicMock(read=lambda: b'{"retry_after": 0.01}')
        )
        ok_resp = MagicMock()
        ok_resp.getcode.return_value = 204
        ok_resp.__enter__ = lambda s: s
        ok_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", side_effect=[rate_limited, ok_resp]):
            result = _send_webhook_raw("https://test", {"content": "hi"})
            assert result is True

class TestArbitrageAndMiddleAlerts:
    def _opp(self, **overrides):
        opp = {
            "player_name": "Test Pitcher", "market_type": "pitching_strikeouts_ou",
            "matchup": "Away @ Home",
            "side_a": "OVER", "side_a_price": 110, "side_a_sportsbook": "BookA",
            "side_b": "UNDER", "side_b_price": 130, "side_b_sportsbook": "BookB",
            "guaranteed_roi_pct": 8.5,
        }
        opp.update(overrides)
        return opp

    def test_arbitrage_alert_no_webhooks(self):
        from src.discord_delivery import deliver_arbitrage_alerts
        result = deliver_arbitrage_alerts([self._opp()], [])
        assert result["sent"] == 0

    def test_arbitrage_alert_no_opportunities(self):
        from src.discord_delivery import deliver_arbitrage_alerts
        result = deliver_arbitrage_alerts([], ["https://discord.com/api/webhooks/test"])
        assert result["sent"] == 0

    def test_arbitrage_alert_sends_and_includes_player(self):
        from src.discord_delivery import deliver_arbitrage_alerts
        with patch("src.discord_delivery._send_webhook_raw", return_value=True) as mock:
            result = deliver_arbitrage_alerts(
                [self._opp()], ["https://discord.com/api/webhooks/test"]
            )
            assert result["sent"] == 1
            payload = mock.call_args[0][1]
            assert "Test Pitcher" in payload["content"]

    def test_middle_alert_sends(self):
        from src.discord_delivery import deliver_middle_alerts
        opp = {
            "player_name": "Test Batter", "market_type": "batting_totalBases_ou",
            "matchup": "Away @ Home",
            "over_line": 1.5, "over_sportsbook": "BookA", "over_price": -110,
            "under_line": 2.5, "under_sportsbook": "BookB", "under_price": -110,
            "worst_case_roi_pct": -2.0, "best_case_roi_pct": 90.0,
        }
        with patch("src.discord_delivery._send_webhook_raw", return_value=True) as mock:
            result = deliver_middle_alerts([opp], ["https://discord.com/api/webhooks/test"])
            assert result["sent"] == 1
            payload = mock.call_args[0][1]
            assert "Test Batter" in payload["content"]

    def test_middle_alert_no_webhooks(self):
        from src.discord_delivery import deliver_middle_alerts
        result = deliver_middle_alerts([{"player_name": "x"}], [])
        assert result["sent"] == 0


class TestNewRecommendationAlerts:
    def _make_full_db(self, tmp_path: Path) -> Path:
        from database.db_manager import init_db

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
        conn.execute("INSERT INTO official_picks (recommendation_id, tier, official_rank) "
                     "VALUES ('rec-1', 'OFFICIAL_TRACKED', 1)")
        conn.commit()
        conn.close()
        return db_path

    def test_no_webhooks_configured(self, tmp_path):
        from src.discord_delivery import deliver_new_recommendation_alerts
        db_path = self._make_full_db(tmp_path)
        result = deliver_new_recommendation_alerts(db_path, [])
        assert result["sent"] == 0

    def test_sends_a_new_pick(self, tmp_path):
        from src.discord_delivery import deliver_new_recommendation_alerts
        db_path = self._make_full_db(tmp_path)
        with patch("src.discord_delivery._send_webhook_raw", return_value=True) as mock:
            result = deliver_new_recommendation_alerts(
                db_path, ["https://discord.com/api/webhooks/test"], min_confidence=0, min_ev_pct=0,
            )
            assert result["sent"] == 1
            assert "Judge" in mock.call_args[0][1]["content"]

    def test_does_not_resend_an_already_alerted_pick(self, tmp_path):
        from src.discord_delivery import deliver_new_recommendation_alerts
        db_path = self._make_full_db(tmp_path)
        with patch("src.discord_delivery._send_webhook_raw", return_value=True):
            deliver_new_recommendation_alerts(
                db_path, ["https://discord.com/api/webhooks/test"], min_confidence=0, min_ev_pct=0,
            )
            second = deliver_new_recommendation_alerts(
                db_path, ["https://discord.com/api/webhooks/test"], min_confidence=0, min_ev_pct=0,
            )
        assert second["sent"] == 0

    def test_a_failed_send_is_not_marked_alerted_and_retries_next_time(self, tmp_path):
        from src.discord_delivery import deliver_new_recommendation_alerts
        db_path = self._make_full_db(tmp_path)
        with patch("src.discord_delivery._send_webhook_raw", return_value=False):
            first = deliver_new_recommendation_alerts(
                db_path, ["https://discord.com/api/webhooks/test"], min_confidence=0, min_ev_pct=0,
            )
        assert first["errors"] >= 1
        with patch("src.discord_delivery._send_webhook_raw", return_value=True):
            second = deliver_new_recommendation_alerts(
                db_path, ["https://discord.com/api/webhooks/test"], min_confidence=0, min_ev_pct=0,
            )
        assert second["sent"] == 1


class TestUnalertedRecommendationIdsDedup:
    """database.db_manager's discord alert dedup helpers, used by
    deliver_new_recommendation_alerts above."""

    def test_first_time_ids_are_all_unalerted(self, db_conn):
        from database.db_manager import get_unalerted_recommendation_ids
        result = get_unalerted_recommendation_ids(db_conn, ["rec-1", "rec-2"])
        assert result == ["rec-1", "rec-2"]

    def test_marked_ids_are_excluded_next_time(self, db_conn):
        from database.db_manager import get_unalerted_recommendation_ids, mark_recommendations_alerted
        mark_recommendations_alerted(db_conn, ["rec-1"])
        result = get_unalerted_recommendation_ids(db_conn, ["rec-1", "rec-2"])
        assert result == ["rec-2"]

    def test_works_against_postgres_shaped_dict_only_rows(self, db_conn_dict_rows):
        """Same r[0]-vs-r['col'] bug class as sync_arbitrage_opportunities
        (see tests/conftest.py's db_conn_dict_rows docstring) -- this
        function has its own SELECT that needs the same fix."""
        from database.db_manager import get_unalerted_recommendation_ids, mark_recommendations_alerted
        mark_recommendations_alerted(db_conn_dict_rows, ["rec-1"])
        result = get_unalerted_recommendation_ids(db_conn_dict_rows, ["rec-1", "rec-2"])
        assert result == ["rec-2"]


class TestDeliverNoRecsInDb:
    def test_deliver_no_recs_in_db(self, tmp_path):
        from src.discord_delivery import deliver_recommendations
        db_path = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE historical_recommendations (
                id INTEGER PRIMARY KEY,
                player_name TEXT,
                rec_status TEXT,
                ev_pct REAL,
                yn_implied_prob_adv REAL
            )
        """)
        conn.commit()
        conn.close()
        result = deliver_recommendations(
            db_path, ["https://discord.com/api/webhooks/test"]
        )
        assert result["sent"] == 0


class TestDiscordConsumesOfficialPicksOnly:
    """Launch audit 2026-09-24: the customer site, history, stats and Auto-Bet
    all treat the ACTIVE row of official_picks as THE recommendation; Discord
    must too."""

    def _db(self, tmp_path):
        from database.db_manager import init_db
        path = tmp_path / "official.db"
        init_db(str(path))
        return path, sqlite3.connect(str(path))

    def test_research_and_unofficial_recommendations_are_never_alerted(self, tmp_path):
        from src.discord_delivery import _load_actionable_recommendations
        path, conn = self._db(tmp_path)
        _seed_rec(conn, "official-1", official=True)
        _seed_rec(conn, "research-1", official=False)                     # actionable EV but not official
        conn.close()
        ids = {r["recommendation_id"] for r in _load_actionable_recommendations(path)}
        assert ids == {"official-1"}

    def test_superseded_official_picks_are_not_alerted(self, tmp_path):
        from src.discord_delivery import _load_actionable_recommendations
        path, conn = self._db(tmp_path)
        _seed_rec(conn, "old-pick", pick_status="SUPERSEDED")
        _seed_rec(conn, "new-pick", pick_status="ACTIVE")
        conn.close()
        assert {r["recommendation_id"] for r in _load_actionable_recommendations(path)} == {"new-pick"}

    def test_a_game_that_already_started_is_not_alerted(self, tmp_path):
        from src.discord_delivery import _load_actionable_recommendations
        path, conn = self._db(tmp_path)
        _seed_rec(conn, "started", start_delta_hours=-1)
        _seed_rec(conn, "upcoming", start_delta_hours=2)
        conn.close()
        assert {r["recommendation_id"] for r in _load_actionable_recommendations(path)} == {"upcoming"}

    def test_non_actionable_status_of_an_official_pick_is_not_alerted(self, tmp_path):
        from src.discord_delivery import _load_actionable_recommendations
        path, conn = self._db(tmp_path)
        _seed_rec(conn, "no-edge", status="NO_EDGE")
        conn.close()
        assert _load_actionable_recommendations(path) == []

    def test_end_to_end_only_the_official_pick_reaches_the_webhook_and_only_once(self, tmp_path):
        from src.discord_delivery import deliver_new_recommendation_alerts
        path, conn = self._db(tmp_path)
        _seed_rec(conn, "official-1", player="Official Guy")
        _seed_rec(conn, "research-1", player="Research Guy", official=False)
        conn.close()
        sent = []
        with patch("src.discord_delivery._send_webhook_raw", side_effect=lambda url, payload, *a, **k: sent.append(str(payload)) or True):
            first = deliver_new_recommendation_alerts(path, ["https://discord.com/api/webhooks/1/x"])
            second = deliver_new_recommendation_alerts(path, ["https://discord.com/api/webhooks/1/x"])
        blob = " ".join(sent)
        assert "Official Guy" in blob and "Research Guy" not in blob
        assert first["sent"] == 1 and second["sent"] == 0             # dedup: the same pick is never re-sent
