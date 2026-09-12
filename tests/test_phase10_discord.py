"""Tests for Phase 10 Part D: Discord Delivery."""

from __future__ import annotations

import json
import sqlite3
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestDiscordDelivery:

    def _make_db_with_recs(self, tmp_path: Path) -> Path:
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE historical_recommendations (
                id INTEGER PRIMARY KEY,
                player_name TEXT,
                event_id TEXT,
                market_type TEXT,
                sportsbook TEXT,
                offered_american_odds INTEGER,
                ev_pct REAL,
                yn_implied_prob_adv REAL,
                rec_status TEXT,
                fingerprint TEXT
            )
        """)
        conn.execute(
            "INSERT INTO historical_recommendations VALUES (1, 'Judge', 'NYY-BOS', 'strikeouts', "
            "'DK', -110, 0.05, NULL, 'STRONG_EDGE', 'fp_abc123')"
        )
        conn.execute(
            "INSERT INTO historical_recommendations VALUES (2, 'Ohtani', 'LAD-SF', 'home_runs', "
            "'FD', 350, NULL, 0.06, 'POSITIVE_EDGE', 'fp_def456')"
        )
        conn.commit()
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
        from src.discord_delivery import deliver_recommendations
        db_path = tmp_path / "low_ev.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE historical_recommendations (
                id INTEGER PRIMARY KEY,
                player_name TEXT,
                event_id TEXT,
                market_type TEXT,
                sportsbook TEXT,
                offered_american_odds INTEGER,
                ev_pct REAL,
                yn_implied_prob_adv REAL,
                rec_status TEXT,
                fingerprint TEXT
            )
        """)
        conn.execute(
            "INSERT INTO historical_recommendations VALUES (1, 'Judge', 'E1', 'strikeouts', "
            "'DK', -110, 0.01, 0.01, 'STRONG_EDGE', 'fp_abc')"
        )
        conn.commit()
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
