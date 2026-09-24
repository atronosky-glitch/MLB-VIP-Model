"""END-TO-END Auto-Bet (PAPER) for BOTH platforms through the production
worker entry point, with no real order possible:

  official recommendation -> customer settings -> exact contract mapping
  (real-structure Kalshi / Polymarket payloads) -> order book -> EV -> risk
  -> paper execution -> My Performance (open, then settled) -> a second
  worker pass after a "restart" places nothing new.

Market payloads are the real captured structures with dates regenerated
relative to "now" so the test never expires. The Polymarket payload is the
real NFL structure re-labelled MLB (Polymarket US lists no MLB game markets
today, and NFL moneyline is intentionally disabled by the tie guard) -- the
mapping/evaluation/execution code under test is identical.
"""

from __future__ import annotations

import base64
import copy
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

import database.db_manager as dbm
import src.customer_kalshi as ck
import src.customer_polymarket as cp
import src.execution.customer_autobet as autobet
from database.db_manager import init_db
from src.credential_encryption import ENCRYPTION_KEY_ENV_VAR, generate_encryption_key
from src.customer_accounts import sign_up
from src.customer_performance import get_customer_performance
from src.execution.base import Market, Orderbook
from src.execution.kalshi import KalshiProvider
from src.execution.kalshi_mapping import EASTERN
from src.execution.polymarket_us import PolymarketUSProvider
from src.production_config import load_config

FIX = Path(__file__).parent / "fixtures"
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
REC_ID = "rec-e2e-1"


def _game_time():
    """Tomorrow 7:05 PM Eastern (a plausible MLB start), as ET-local and UTC."""
    et = (datetime.now(EASTERN) + timedelta(days=1)).replace(hour=19, minute=5, second=0, microsecond=0)
    return et, et.astimezone(timezone.utc)


def _kalshi_markets():
    et, _utc = _game_time()
    base = json.loads((FIX / "kalshi_game_markets.json").read_text(encoding="utf-8"))["KXMLBGAME"][0]
    event = f"KXMLBGAME-{et:%y}{MONTHS[et.month - 1]}{et:%d}1905AZSD"
    out = []
    for code, label in (("AZ", "Arizona"), ("SD", "San Diego")):
        raw = copy.deepcopy(base)
        raw.update(event_ticker=event, ticker=f"{event}-{code}", yes_sub_title=label, no_sub_title=label,
                   title=f"{label} wins", status="active")
        out.append(Market(id=raw["ticker"], title=label, status="active", raw=raw))
    return out


def _polymarket_market():
    _et, utc = _game_time()
    base = json.loads((FIX / "polymarket_us_moneyline_markets.json").read_text(encoding="utf-8"))["away_is_yes"][0]
    raw = copy.deepcopy(base)
    slug = f"aec-mlb-az-sd-{utc:%Y-%m-%d}"
    raw.update(slug=slug, closed=False, active=True, gameStartTime=utc.strftime("%Y-%m-%dT%H:%M:%SZ"))
    for side, name in zip(raw["marketSides"], ("Arizona Diamondbacks", "San Diego Padres")):
        side["identifier"] = slug
        side["team"].update(name=name, league="mlb")
    return Market(id=slug, title="Arizona vs. San Diego", status="active", raw=raw)


def _pem():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


def _b64():
    raw = ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    return base64.b64encode(raw).decode()


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setenv(ENCRYPTION_KEY_ENV_VAR, generate_encryption_key())
    monkeypatch.delenv("DATABASE_URL", raising=False)
    path = str(tmp_path / "autobet.db")
    init_db(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _et, utc = _game_time()
    now = datetime.now(timezone.utc)
    conn.execute(
        """INSERT INTO historical_recommendations (recommendation_id, fingerprint, event_id, player_id,
               player_name, matchup, market_type, market_form, period, line, side, sportsbook,
               offered_american_odds, offered_decimal_odds, offered_implied_prob, fair_prob, ev_pct,
               model_score, rec_status, rec_eligible, recommendation_tier, scan_timestamp, event_start_time,
               league, sport)
           VALUES (?, 'fp-e2e', 'EV1', 'GAME', NULL, 'Arizona Diamondbacks @ San Diego Padres',
                   'game_moneyline', 'ml', 'full_game', NULL, 'AWAY', 'Pinnacle', 120, 2.2, 0.4545, 0.62, 9.0,
                   8.5, 'STRONG_EDGE', 1, 'OFFICIAL_TRACKED', ?, ?, 'MLB', 'baseball')""",
        (REC_ID, now.isoformat(), utc.isoformat()),
    )
    conn.execute("INSERT INTO official_picks (recommendation_id, tier, official_rank, pick_status) "
                 "VALUES (?, 'OFFICIAL_TRACKED', 1, 'ACTIVE')", (REC_ID,))
    conn.commit()

    a = sign_up(conn, "a@example.com", None, "correct-horse-1", False)
    b = sign_up(conn, "b@example.com", None, "correct-horse-1", False)
    with mock.patch.object(ck.KalshiProvider, "health_check", return_value=mock.MagicMock(ok=True)), \
         mock.patch.object(cp.PolymarketUSProvider, "health_check", return_value=mock.MagicMock(ok=True)):
        for acct, plat in ((a, ck), (a, cp), (b, ck)):
            key = _pem() if plat is ck else _b64()
            ok, msg = plat.connect_account(conn, acct.account_id, f"key-{uuid.uuid4().hex[:8]}", key)
            assert ok, msg
    for acct, plat in ((a, ck), (a, cp), (b, ck)):
        plat.save_risk_settings(conn, acct.account_id, **plat.DEFAULT_RISK_SETTINGS)
        ok, msg = plat.enable_autobet(conn, acct.account_id)          # PAPER: live_execution stays 0
        assert ok, msg

    config = load_config()
    config.database_path = path
    return SimpleWorld(path, conn, config, a, b)


class SimpleWorld:
    def __init__(self, path, conn, config, a, b):
        self.path, self.conn, self.config, self.a, self.b = path, conn, config, a, b


def _providers():
    """Real provider classes (real parsing, real fee formulas) with only the
    network methods replaced by deterministic data. Any write raises."""
    kalshi = KalshiProvider(api_key_id="scan-only", private_key_pem=_pem())
    poly = PolymarketUSProvider(api_key_id="scan-only", private_key_b64=_b64())

    def boom(*a, **k):
        raise AssertionError("a provider WRITE was attempted during a paper run")

    for p in (kalshi, poly):
        p.place_order = boom
        p.cancel_order = boom
        p._submit_authorized_order = boom
    kalshi.get_game_markets = lambda leagues=None: _kalshi_markets()
    # yes bids 0.54 / no bids 0.44  ->  YES ask 0.56, tight spread, deep book
    kalshi.get_orderbook = lambda market_id: Orderbook(market_id=market_id, bids=[(0.54, 800.0)], asks=[(0.44, 800.0)], raw={})
    poly.get_game_markets = lambda leagues=None: [_polymarket_market()]
    poly.get_orderbook = lambda market_id: Orderbook(market_id=market_id, bids=[(0.54, 800.0)], asks=[(0.56, 800.0)], raw={})
    return {"kalshi": kalshi, "polymarket_us": poly}


def _run_pass(world):
    providers = _providers()
    with mock.patch.object(autobet, "_build_scanning_only_provider", side_effect=lambda platform: providers[platform]):
        return autobet.run_customer_autobet_pass(world.config)


def _executions(world):
    conn = sqlite3.connect(world.path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM customer_autobet_executions ORDER BY created_at").fetchall()]
    finally:
        conn.close()


class TestPaperEndToEnd:
    def test_official_pick_becomes_exact_paper_executions_on_both_platforms(self, world):
        result = _run_pass(world)
        assert result["executed"] == 3 and result["failed"] == 0, result
        rows = _executions(world)
        by = {(r["account_id"], r["platform"]): r for r in rows if r["status"] == "EXECUTED"}
        assert set(by) == {(world.a.account_id, "kalshi"), (world.a.account_id, "polymarket_us"),
                           (world.b.account_id, "kalshi")}
        for row in by.values():
            assert row["mode"] == "PAPER" and row["fill_source"] == "SIMULATED"
            assert row["recommendation_id"] == REC_ID
            assert row["side"] == "YES"                                   # AWAY (Arizona) -> Arizona's YES contract
            assert row["filled_quantity"] > 0 and row["avg_fill_price"] == pytest.approx(0.56, abs=0.005)
            assert row["fees_source"] == "ESTIMATED" and row["fees_usd"] >= 0
            cost = row["filled_quantity"] * row["avg_fill_price"] + row["fees_usd"]
            assert cost <= row["stake_usd"] + 1e-6                        # never above the approved stake

    def test_kalshi_order_is_on_the_exact_arizona_contract(self, world):
        _run_pass(world)
        conn = sqlite3.connect(world.path)
        n_claims = conn.execute("SELECT COUNT(*) FROM customer_autobet_platform_claims").fetchone()[0]
        conn.close()
        assert n_claims == 3                                              # (a,kalshi) (a,poly) (b,kalshi)

    def test_paper_run_touches_no_live_state_and_no_provider_write(self, world):
        _run_pass(world)                                                  # a write would raise inside _providers()
        conn = sqlite3.connect(world.path)
        for table in ("prepared_live_orders", "execution_authorizations", "live_orders", "live_positions"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
        conn.close()

    def test_my_performance_open_then_settled_uses_the_recorded_paper_fill(self, world):
        _run_pass(world)
        perf = get_customer_performance(world.conn, world.a.account_id, mode="PAPER")
        assert perf["total_bets"] == 2 and perf["open_positions"] == 2 and perf["realized_pnl_usd"] == 0
        assert perf["simulated"] is True
        assert get_customer_performance(world.conn, world.a.account_id, mode="LIVE")["total_bets"] == 0

        rows = [r for r in _executions(world) if r["account_id"] == world.a.account_id and r["status"] == "EXECUTED"]
        world.conn.execute(
            "INSERT INTO market_settlements (settlement_id, recommendation_id, settlement_status, settled_at) "
            "VALUES (?, ?, 'WIN', ?)", (str(uuid.uuid4()), REC_ID, datetime.now(timezone.utc).isoformat()))
        world.conn.commit()
        settled = get_customer_performance(world.conn, world.a.account_id, mode="PAPER")
        expected = sum(r["filled_quantity"] * 1.0 - r["filled_quantity"] * r["avg_fill_price"] - r["fees_usd"] for r in rows)
        assert settled["wins"] == 2 and settled["open_positions"] == 0
        assert settled["realized_pnl_usd"] == pytest.approx(expected)
        assert settled["pnl_basis"] == "NET_ESTIMATED_FEES"

    def test_customer_b_sees_none_of_customer_as_polymarket_activity(self, world):
        _run_pass(world)
        perf_b = get_customer_performance(world.conn, world.b.account_id, mode="PAPER")
        assert perf_b["total_bets"] == 1                                   # only b's own Kalshi bet
        assert perf_b["realized_pnl_by_platform"]["polymarket_us"] == 0

    def test_restart_and_rerun_place_nothing_new(self, world):
        _run_pass(world)
        before = len(_executions(world))
        second = _run_pass(world)                                          # fresh connection == worker restart
        assert second["executed"] == 0
        assert len(_executions(world)) == before                           # no duplicate bets, none lost

    def test_a_non_official_recommendation_is_never_executed(self, world):
        conn = sqlite3.connect(world.path)
        conn.execute("UPDATE official_picks SET pick_status = 'SUPERSEDED'")
        conn.commit()
        conn.close()
        assert _run_pass(world)["executed"] == 0
        assert _executions(world) == []

    def test_a_game_that_already_started_is_never_executed(self, world):
        conn = sqlite3.connect(world.path)
        conn.execute("UPDATE historical_recommendations SET event_start_time = ?",
                     ((datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),))
        conn.commit()
        conn.close()
        assert _run_pass(world)["executed"] == 0

    def test_a_wrong_team_market_is_never_traded(self, world):
        conn = sqlite3.connect(world.path)
        conn.execute("UPDATE historical_recommendations SET matchup = 'Colorado Rockies @ Kansas City Royals'")
        conn.commit()
        conn.close()
        assert _run_pass(world)["executed"] == 0

    def test_live_gates_default_off_and_customer_live_toggle_cannot_override(self, world):
        cfg = world.config
        assert cfg.live_trading_enabled is False and cfg.kalshi_live_enabled is False
        assert cfg.polymarket_us_live_enabled is False
        ck.set_live_execution(world.conn, world.a.account_id, True)        # customer asks for LIVE
        providers = _providers()
        with mock.patch.object(autobet, "_build_scanning_only_provider", side_effect=lambda p: providers[p]):
            autobet.run_customer_autobet_pass(cfg)                          # server flags are OFF
        rows = [r for r in _executions(world) if r["account_id"] == world.a.account_id and r["platform"] == "kalshi"]
        assert rows and all(r["status"] != "EXECUTED" for r in rows)
        assert any("LIVE_TRADING_DISABLED" in (r.get("skip_reason") or "") for r in rows)
