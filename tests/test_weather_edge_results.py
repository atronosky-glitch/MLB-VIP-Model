"""Weather Edge: settlement, paper P&L, performance metrics, and the no-look-ahead backtest."""

import copy
import random
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from src.weather_edge import backtest as bt
from src.weather_edge import performance, settlement, store
from src.weather_edge.config import WeatherEdgeConfig
from src.weather_edge.models import Bin
from tests.weather_edge_helpers import NOW, one_event_contract

LATER = NOW + timedelta(days=2)


def seed_trade(conn, market_id="T-1", side="YES", qty=40, cost=20.70, bin_=(72, 73), platform="kalshi",
               target=date(2026, 10, 10), event_key="EV-1", p_yes=0.4, lead=1, mid=0.35, baseline=0.3):
    c = one_event_contract(platform=platform, market_id=market_id, event_key=event_key, cfg_bin_lo=bin_[0],
                           cfg_bin_hi=bin_[1], target_date=target)
    store.upsert_contract(conn, c, NOW)
    pid, _ = store.insert_prediction(conn, {
        "prediction_key": f"{platform}|{market_id}|{lead}", "platform": platform, "market_id": market_id,
        "created_at": NOW.isoformat(), "model_version": "v", "lead_days": lead, "p_yes": p_yes,
        "market_yes_mid": mid, "baseline_p_yes": baseline})
    sid, _ = store.insert_signal(conn, {
        "signal_key": f"{platform}|{market_id}|{side}", "prediction_id": pid, "platform": platform, "market_id": market_id,
        "created_at": NOW.isoformat(), "side": side, "decision": "SIGNAL", "net_ev": 0.1, "net_ev_pct": 20.0})
    store.insert_paper_trade(conn, {"signal_id": sid, "platform": platform, "market_id": market_id, "side": side,
                                    "qty": qty, "total_cost": cost, "p_win": 0.6, "opened_at": NOW.isoformat()})
    conn.commit()


class FakeKalshiMarkets:
    def __init__(self, by_ticker):
        self.by_ticker = by_ticker

    def get_market(self, ticker):
        m = self.by_ticker[ticker]
        if isinstance(m, Exception):
            raise m
        return m


class FakePolyEvents:
    def __init__(self, events):
        self.events = events

    def get_event(self, slug):
        return self.events.get(slug)


class TestSettlement:
    def test_winning_yes_and_losing_no_get_exact_paper_pnl(self, db_conn):
        seed_trade(db_conn, "T-1", "YES", 40, 20.70)
        seed_trade(db_conn, "T-2", "NO", 10, 6.0, bin_=(72, 73), event_key="EV-2")
        k = FakeKalshiMarkets({"T-1": {"result": "yes", "expiration_value": "73.00"}, "T-2": {"result": "yes", "expiration_value": "73.00"}})
        out = settlement.run_settlement(db_conn, LATER, kalshi=k, polymarket=FakePolyEvents({}))
        assert out["settled"] == 2 and out["disputed"] == 0
        t = {r["market_id"]: r for r in db_conn.execute("SELECT * FROM weather_paper_trades")}
        assert t["T-1"]["won"] == 1 and t["T-1"]["pnl"] == pytest.approx(40 - 20.70)
        assert t["T-2"]["won"] == 0 and t["T-2"]["pnl"] == pytest.approx(-6.0)
        assert all(r["status"] == "settled" for r in t.values())
        pred = db_conn.execute("SELECT outcome_yes FROM weather_predictions WHERE market_id = 'T-1'").fetchone()
        assert pred["outcome_yes"] == 1
        c = store.get_contract(db_conn, "kalshi", "T-1")
        assert c["settled_result"] == "yes" and c["settled_value"] == 73.0 and c["status"] == "settled"

    def test_rerun_is_idempotent(self, db_conn):
        seed_trade(db_conn)
        k = FakeKalshiMarkets({"T-1": {"result": "no", "expiration_value": "70.00"}})
        settlement.run_settlement(db_conn, LATER, kalshi=k, polymarket=FakePolyEvents({}))
        again = settlement.run_settlement(db_conn, LATER, kalshi=k, polymarket=FakePolyEvents({}))
        assert again["checked"] == 0
        assert db_conn.execute("SELECT pnl FROM weather_paper_trades").fetchone()["pnl"] == pytest.approx(-20.70)

    def test_unfinished_markets_stay_open_and_nothing_is_inferred(self, db_conn):
        seed_trade(db_conn)
        k = FakeKalshiMarkets({"T-1": {"result": "", "expiration_value": ""}})
        out = settlement.run_settlement(db_conn, LATER, kalshi=k, polymarket=FakePolyEvents({}))
        assert out["pending"] == 1 and out["settled"] == 0
        assert db_conn.execute("SELECT status FROM weather_paper_trades").fetchone()["status"] == "open"

    def test_result_that_contradicts_the_official_value_is_disputed_and_not_settled(self, db_conn):
        seed_trade(db_conn)        # bin 72-73
        k = FakeKalshiMarkets({"T-1": {"result": "no", "expiration_value": "73.00"}})     # 73 is inside the bin yet "no"
        out = settlement.run_settlement(db_conn, LATER, kalshi=k, polymarket=FakePolyEvents({}))
        assert out["disputed"] == 1 and out["settled"] == 0
        assert db_conn.execute("SELECT status FROM weather_paper_trades").fetchone()["status"] == "open"
        assert db_conn.execute("SELECT outcome_yes FROM weather_predictions").fetchone()["outcome_yes"] is None
        assert store.get_contract(db_conn, "kalshi", "T-1")["settled_result"] == "disputed"

    def test_provider_error_on_one_contract_does_not_block_others(self, db_conn):
        seed_trade(db_conn, "T-1")
        seed_trade(db_conn, "T-2", event_key="EV-2")
        k = FakeKalshiMarkets({"T-1": RuntimeError("boom"), "T-2": {"result": "yes", "expiration_value": "72.00"}})
        out = settlement.run_settlement(db_conn, LATER, kalshi=k, polymarket=FakePolyEvents({}))
        assert out["settled"] == 1 and len(out["errors"]) == 1

    def test_future_target_dates_are_not_checked(self, db_conn):
        seed_trade(db_conn, target=date(2026, 10, 14))
        out = settlement.run_settlement(db_conn, LATER, kalshi=FakeKalshiMarkets({}), polymarket=FakePolyEvents({}))
        assert out["checked"] == 0

    def test_polymarket_resolution_requires_uma_resolved(self, db_conn):
        seed_trade(db_conn, "0xabc", platform="polymarket", event_key="slug-1")
        ev = {"markets": [{"conditionId": "0xabc", "closed": True, "umaResolutionStatus": "resolved", "outcomePrices": '["1", "0"]'}]}
        out = settlement.run_settlement(db_conn, LATER, kalshi=FakeKalshiMarkets({}), polymarket=FakePolyEvents({"slug-1": ev}))
        assert out["settled"] == 1
        assert db_conn.execute("SELECT won FROM weather_paper_trades").fetchone()["won"] == 1

    def test_polymarket_unresolved_event_is_pending(self, db_conn):
        seed_trade(db_conn, "0xabc", platform="polymarket", event_key="slug-1")
        ev = {"markets": [{"conditionId": "0xabc", "closed": False, "outcomePrices": '["0.4", "0.6"]'}]}
        out = settlement.run_settlement(db_conn, LATER, kalshi=FakeKalshiMarkets({}), polymarket=FakePolyEvents({"slug-1": ev}))
        assert out["pending"] == 1

    def test_value_consistency_helper(self):
        assert settlement.value_consistent(72, 73, 73.0, "yes") and settlement.value_consistent(72, 73, 74.0, "no")
        assert not settlement.value_consistent(72, 73, 74.0, "yes")
        assert settlement.value_consistent(None, 70, 65.0, "yes") and settlement.value_consistent(72, 73, None, "yes")


class TestPerformance:
    def test_empty_database_reports_zero_samples_not_made_up_numbers(self, db_conn):
        assert performance.paper_performance(db_conn)["overall"] == {"n": 0}
        assert performance.prediction_scores(db_conn)["overall"] == {"n": 0}

    def test_scores_and_trade_stats_from_settled_rows(self, db_conn):
        seed_trade(db_conn, "T-1", "YES", 40, 20.0, p_yes=0.8, mid=0.5, baseline=0.6)
        seed_trade(db_conn, "T-2", "YES", 10, 5.0, p_yes=0.2, mid=0.5, baseline=0.4, event_key="EV-2")
        k = FakeKalshiMarkets({"T-1": {"result": "yes", "expiration_value": "73"}, "T-2": {"result": "no", "expiration_value": "70"}})
        settlement.run_settlement(db_conn, LATER, kalshi=k, polymarket=FakePolyEvents({}))
        sc = performance.prediction_scores(db_conn)["overall"]
        assert sc["n"] == 2 and sc["brier_model"] == pytest.approx((0.04 + 0.04) / 2)
        assert sc["brier_market"] == pytest.approx(0.25) and sc["brier_baseline"] == pytest.approx((0.16 + 0.16) / 2)
        assert not sc["reliable"]                                  # n < 30: flagged as thin
        tp = performance.paper_performance(db_conn)
        o = tp["overall"]
        assert o["n"] == 2 and o["wins"] == 1 and o["pnl"] == pytest.approx((40 - 20.0) + (-5.0))
        assert o["roi_pct"] == pytest.approx(15.0 / 25.0 * 100) and "PAPER" in tp["label"] and not o["reliable"]

    def test_max_drawdown(self, db_conn):
        rows = [{"pnl": 10, "total_cost": 5, "won": 1, "net_ev": .1, "qty": 10}, {"pnl": -4, "total_cost": 5, "won": 0, "net_ev": .1, "qty": 10},
                {"pnl": -3, "total_cost": 5, "won": 0, "net_ev": .1, "qty": 10}, {"pnl": 5, "total_cost": 5, "won": 1, "net_ev": .1, "qty": 10}]
        assert performance._trade_stats(rows)["max_drawdown"] == 7

    def test_repeated_predictions_for_one_contract_count_once_per_lead(self, db_conn):
        seed_trade(db_conn, lead=1)
        for i in range(5):
            store.insert_prediction(db_conn, {"prediction_key": f"dup{i}", "platform": "kalshi", "market_id": "T-1",
                                              "created_at": (NOW + timedelta(minutes=i + 1)).isoformat(), "model_version": "v",
                                              "lead_days": 1, "p_yes": 0.9})
        settlement.run_settlement(db_conn, LATER, kalshi=FakeKalshiMarkets({"T-1": {"result": "yes", "expiration_value": "73"}}),
                                  polymarket=FakePolyEvents({}))
        assert performance.prediction_scores(db_conn)["overall"]["n"] == 1

    def test_reliability_table_buckets(self):
        rows = [{"p_yes": 0.05, "outcome_yes": 0}, {"p_yes": 0.07, "outcome_yes": 1}, {"p_yes": 0.95, "outcome_yes": 1}]
        t = performance.reliability_table(rows)
        assert [r["n"] for r in t] == [2, 1] and t[0]["observed_freq"] == 0.5


# ------------------------------------------------------------------------------ backtest
CFG = WeatherEdgeConfig(min_net_ev_pct=5.0, max_spread=0.10)


def make_days(n=80, seed=1, priced="naive", forecast_noise=1.5, market_noise=0.0):
    """A world where the forecast is good (error std ~forecast_noise) and contract prices are either
    'naive' (a flat 0.20 on every bin) or 'efficient' (the true probability)."""
    rnd = random.Random(seed)
    out = []
    start = date(2026, 6, 1)
    from src.weather_edge.probability import bin_probability, point_distribution
    for i in range(n):
        day = start + timedelta(days=i)
        fc = 70 + rnd.uniform(-5, 5)
        obs = round(fc + rnd.gauss(0, forecast_noise))
        d = bt.DayRecord("KNYC", "high", day, fc, float(obs))
        for lo in range(60, 82, 2):
            b = Bin(lo, lo + 1)
            true_p = bin_probability(point_distribution(fc, forecast_noise), b)
            mid = 0.20 if priced == "naive" else min(max(true_p + rnd.uniform(-market_noise, market_noise), 0.02), 0.98)
            d.contracts.append(bt.DayContract(f"M{i}-{lo}", b, D(str(round(max(mid - 0.02, 0.01), 2))),
                                              D(str(round(min(mid + 0.02, 0.99), 2))), "yes" if b.contains(obs) else "no"))
        out.append(d)
    return out


class TestBacktest:
    def test_days_without_enough_prior_history_are_skipped_not_guessed(self):
        r = bt.simulate(make_days(40), CFG, min_history=30)
        assert r["days_skipped_insufficient_history"] >= 30 and r["days_simulated"] <= 10
        assert bt.simulate(make_days(10), CFG, min_history=30)["n_bets"] == 0

    def test_result_is_labeled_simulated_with_caveats(self):
        r = bt.simulate(make_days(60), CFG)
        assert "RECONSTRUCTED BACKTEST" in r["label"] and "Not real trading results" in r["label"] and len(r["caveats"]) >= 3

    def test_no_look_ahead_future_data_cannot_change_earlier_decisions(self):
        days = make_days(70)
        base = bt.simulate(days, CFG)
        base_bets = {(b["day"], b["market_id"], b["side"]) for b in _bets(days)}
        # wreck every day after the cutoff: absurd settled values and forecasts
        cutoff = days[50].day
        mutated = copy.deepcopy(days)
        for d in mutated:
            if d.day > cutoff:
                d.settled_value += 40
                d.forecast_f -= 15
                for c in d.contracts:
                    c.result = "no" if c.result == "yes" else "yes"
        mut_bets = {(b["day"], b["market_id"], b["side"]) for b in _bets(mutated)}
        early = lambda s: {x for x in s if x[0] <= cutoff}
        assert early(base_bets) == early(mut_bets) and early(base_bets)

    def test_calibration_for_a_day_excludes_that_day_and_the_day_before(self):
        days = make_days(45)
        target = days[40]
        # poison only the day immediately before and the day itself: decisions for `target` must not change
        base = {(b["market_id"], b["side"]) for b in _bets(days) if b["day"] == target.day}
        mutated = copy.deepcopy(days)
        for d in mutated[39:41]:
            d.settled_value += 30
        after = {(b["market_id"], b["side"]) for b in _bets(mutated) if b["day"] == target.day}
        assert base and base == after

    def test_naive_prices_with_a_good_forecast_find_profitable_bets(self):
        r = bt.simulate(make_days(120, priced="naive"), CFG)
        assert r["n_bets"] > 20 and r["roi_pct"] > 0 and r["win_rate"] is not None and r["max_drawdown"] >= 0
        assert r["brier_model"] < r["brier_market"]

    def test_efficient_prices_leave_no_edge_after_fees(self):
        r = bt.simulate(make_days(120, priced="efficient"), CFG)
        assert r["n_bets"] == 0 or r["roi_pct"] <= 10

    def test_event_exposure_cap_limits_bets_per_day(self):
        capped = WeatherEdgeConfig(min_net_ev_pct=5.0, max_event_exposure_usd=30.0, paper_stake_usd=25.0, max_spread=0.10)
        r = bt.simulate(make_days(100, priced="naive"), capped)
        from collections import Counter
        per_day = Counter(b["day"] for b in _bets(make_days(100, priced="naive"), capped))
        assert max(per_day.values()) <= 1 and r["n_bets"] > 0

    def test_candle_quote_never_uses_a_candle_after_the_decision_time(self):
        candles = [{"end_period_ts": 100, "yes_bid": {"close_dollars": "0.10"}, "yes_ask": {"close_dollars": "0.14"}},
                   {"end_period_ts": 200, "yes_bid": {"close_dollars": "0.50"}, "yes_ask": {"close_dollars": "0.60"}}]
        assert bt._candle_quote(candles, 150) == (D("0.10"), D("0.14"))
        assert bt._candle_quote(candles, 50) == (None, None)
        assert bt._candle_quote([{"end_period_ts": 10, "yes_bid": {"close_dollars": "0.0000"}, "yes_ask": {"close_dollars": "0.03"}}], 99) == (None, D("0.03"))

    def test_run_backtest_persists_a_labeled_result(self, db_conn, monkeypatch):
        monkeypatch.setattr(bt, "assemble_history", lambda *a, **k: (make_days(60), {"errors": []}))
        res = bt.run_backtest(db_conn, object(), {}, CFG, now=NOW)
        row = store.latest_backtests(db_conn, 1)[0]
        assert row["label"] == "reconstructed-walk-forward" and "RECONSTRUCTED" in row["summary_json"] and res["n_bets"] >= 0


def _bets(days, cfg=CFG):
    """Bets list via the public API (simulate returns a summary; reuse its internals through monkeypatch-free capture)."""
    captured = []
    orig = bt._summarize

    def spy(bets, *a, **k):
        captured.extend(bets)
        return orig(bets, *a, **k)
    bt._summarize = spy
    try:
        bt.simulate(days, cfg)
    finally:
        bt._summarize = orig
    return captured
