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
            "'DK', -110, 0.05, NULL, 'BET', 'fp_abc123')"
        )
        conn.execute(
            "INSERT INTO historical_recommendations VALUES (2, 'Ohtani', 'LAD-SF', 'home_runs', "
            "'FD', 350, NULL, 0.06, 'LEAN', 'fp_def456')"
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
            "'DK', -110, 0.01, 0.01, 'BET', 'fp_abc')"
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
