"""Every path that can place a real order (customer Auto-Bet, live-scan) loads
ONLY ACTIVE official picks whose game has not started; the read-only analysis
CLIs keep their broader view."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from database.db_manager import init_db
from src.execution.cli import _load_actionable_rows


class _Cfg:
    def __init__(self, path):
        self.database_path = path

    def execution_allowed_rec_statuses_list(self):
        return ["STRONG_EDGE", "POSITIVE_EDGE"]


def _rec(conn, rec_id, *, official, pick_status="ACTIVE", status="STRONG_EDGE", start_hours=3):
    start = (datetime.now(timezone.utc) + timedelta(hours=start_hours)).isoformat() if start_hours is not None else None
    conn.execute(
        """INSERT INTO historical_recommendations (recommendation_id, fingerprint, event_id, player_id, player_name,
               market_type, market_form, period, line, side, sportsbook, offered_american_odds, offered_decimal_odds,
               offered_implied_prob, ev_pct, rec_status, rec_eligible, scan_timestamp, event_start_time)
           VALUES (?, ?, 'E', 'P', 'X', 'game_moneyline', 'ml', 'full_game', NULL, 'AWAY', 'DK', -110, 1.9, 0.52,
                   5.0, ?, 1, ?, ?)""",
        (rec_id, f"fp-{rec_id}", status, datetime.now(timezone.utc).isoformat(), start),
    )
    if official:
        conn.execute("INSERT INTO official_picks (recommendation_id, tier, official_rank, pick_status) "
                     "VALUES (?, 'OFFICIAL_TRACKED', 1, ?)", (rec_id, pick_status))
    conn.commit()


@pytest.fixture()
def cfg(tmp_path):
    path = str(tmp_path / "x.db")
    init_db(path)
    conn = sqlite3.connect(path)
    _rec(conn, "official-ok", official=True)
    _rec(conn, "research", official=False)
    _rec(conn, "superseded", official=True, pick_status="SUPERSEDED")
    _rec(conn, "started", official=True, start_hours=-2)
    _rec(conn, "no-start-time", official=True, start_hours=None)
    _rec(conn, "no-edge", official=True, status="NO_EDGE")
    conn.close()
    return _Cfg(path)


def test_official_only_returns_just_the_active_upcoming_official_pick(cfg):
    ids = {r["recommendation_id"] for r in _load_actionable_rows(cfg, official_only=True)}
    assert ids == {"official-ok"}


def test_default_analysis_view_is_unchanged_and_broader(cfg):
    ids = {r["recommendation_id"] for r in _load_actionable_rows(cfg)}
    assert {"official-ok", "research", "superseded", "started"} <= ids
    assert "no-edge" not in ids


def test_customer_autobet_and_live_scan_request_official_only():
    from pathlib import Path
    assert "_load_actionable_rows(config, official_only=True)" in Path("src/execution/customer_autobet.py").read_text(encoding="utf-8")
    assert "_load_actionable_rows(config, official_only=True)" in Path("src/execution/live_cli.py").read_text(encoding="utf-8")
