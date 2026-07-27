"""Tests for Phase 10 Part C: Google Sheets Export."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


class TestExportSheets:

    def test_load_recommendations_empty(self, tmp_path):
        from src.export_sheets import _load_recommendations
        db_path = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db_path))
        conn.commit()
        conn.close()
        rows = _load_recommendations(db_path)
        assert rows == []

    def test_load_recommendations_no_table(self, tmp_path):
        from src.export_sheets import _load_recommendations
        db_path = tmp_path / "notables.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE something_else (id INTEGER)")
        conn.commit()
        conn.close()
        rows = _load_recommendations(db_path)
        assert rows == []

    def test_load_recommendations_with_data(self, tmp_path):
        from src.export_sheets import _load_recommendations
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE historical_recommendations (
                observation_timestamp TEXT,
                player_name TEXT,
                event_id TEXT,
                market_type TEXT,
                period TEXT,
                line TEXT,
                side TEXT,
                sportsbook TEXT,
                offered_american_odds INTEGER,
                offered_decimal_odds REAL,
                ev_pct REAL,
                yn_implied_prob_adv REAL,
                confidence_score REAL,
                rec_status TEXT,
                fingerprint TEXT,
                model_score REAL,
                score_version TEXT DEFAULT 'model_score_v1',
                score_explanation TEXT
            )
        """)
        conn.execute(
            "INSERT INTO historical_recommendations VALUES ('2025-07-23', 'Judge', 'NYY-BOS', "
            "'strikeouts', 'game', '0.5', 'Over', 'DK', -110, 2.1, 0.05, NULL, "
            "75, 'BET', 'fp_abc', NULL, NULL, NULL)"
        )
        conn.commit()
        conn.close()
        rows = _load_recommendations(db_path)
        assert len(rows) == 1
        assert rows[0][1] == "Judge"

    def test_ensure_sheet_creates_new(self):
        from src.export_sheets import _ensure_sheet, HEADERS
        mock_sheets = MagicMock()
        mock_sheets.get().execute.return_value = {"sheets": []}

        _ensure_sheet(mock_sheets, "sheet123", "Recommendations")
        mock_sheets.batchUpdate.assert_called_once()

    def test_ensure_sheet_skips_existing(self):
        from src.export_sheets import _ensure_sheet
        mock_sheets = MagicMock()
        mock_sheets.get().execute.return_value = {
            "sheets": [{"properties": {"title": "Recommendations"}}]
        }

        _ensure_sheet(mock_sheets, "sheet123", "Recommendations")
        mock_sheets.batchUpdate.assert_not_called()

    def test_get_existing_fingerprints_empty(self):
        from src.export_sheets import _get_existing_fingerprints
        mock_sheets = MagicMock()
        mock_sheets.values().get().execute.return_value = {"values": []}
        result = _get_existing_fingerprints(mock_sheets, "s1", "Sheet")
        assert result == set()

    def test_get_existing_fingerprints(self):
        from src.export_sheets import _get_existing_fingerprints
        mock_sheets = MagicMock()
        mock_sheets.values().get().execute.return_value = {
            "values": [["fp1"], ["fp2"], ["fp3"]]
        }
        result = _get_existing_fingerprints(mock_sheets, "s1", "Sheet")
        assert result == {"fp1", "fp2", "fp3"}

    def test_export_no_recs(self, tmp_path):
        from src.export_sheets import export_recommendations, _HAS_GOOGLE
        db_path = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db_path))
        conn.commit()
        conn.close()

        if not _HAS_GOOGLE:
            pytest.skip("Google Sheets libraries not installed")

        result = export_recommendations(db_path, "sheet123", str(tmp_path / "creds.json"))
        assert result["rows_upserted"] == 0

    def test_export_missing_spreadsheet_id(self, tmp_path):
        from src.export_sheets import export_recommendations
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.commit()
        conn.close()

        with pytest.raises(ValueError, match="spreadsheet_id"):
            export_recommendations(db_path, "", "creds.json")

    def test_export_missing_credentials(self, tmp_path):
        from src.export_sheets import export_recommendations
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.commit()
        conn.close()

        with pytest.raises(ValueError, match="credentials_path"):
            export_recommendations(db_path, "sheet123", "")

    def test_export_credentials_not_found(self, tmp_path):
        from src.export_sheets import export_recommendations, _HAS_GOOGLE
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        # Create a recommendation so we don't early-return
        conn.execute("""
            CREATE TABLE historical_recommendations (
                observation_timestamp TEXT,
                player_name TEXT,
                event_id TEXT,
                market_type TEXT,
                period TEXT,
                line TEXT,
                side TEXT,
                sportsbook TEXT,
                offered_american_odds INTEGER,
                offered_decimal_odds REAL,
                ev_pct REAL,
                yn_implied_prob_adv REAL,
                confidence_score REAL,
                rec_status TEXT,
                fingerprint TEXT,
                model_score REAL,
                score_version TEXT DEFAULT 'model_score_v1',
                score_explanation TEXT
            )
        """)
        conn.execute(
            "INSERT INTO historical_recommendations VALUES ('t', 'P1', 'E1', 'm', 'g', "
            "'0.5', 'O', 'DK', -110, 2.1, 0.05, NULL, 75, 'BET', 'fp1', NULL, NULL, NULL)"
        )
        conn.commit()
        conn.close()

        if not _HAS_GOOGLE:
            pytest.skip("Google Sheets libraries not installed")

        with pytest.raises(FileNotFoundError):
            export_recommendations(db_path, "sheet123", str(tmp_path / "nope.json"))

    def test_safe_str(self):
        from src.export_sheets import _safe_str
        assert _safe_str(None) == ""
        assert _safe_str(42) == "42"
        assert _safe_str("hello") == "hello"

    def test_headers_complete(self):
        from src.export_sheets import HEADERS
        assert "Player" in HEADERS
        assert "Fingerprint" in HEADERS
        assert "EV %" in HEADERS
        assert len(HEADERS) >= 15
