"""Weather Edge scanner: signals, gates, failure handling, idempotency, alerts."""

from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from src.weather_edge import alerts, store
from src.weather_edge import polymarket_source as pm
from src.weather_edge.config import WeatherEdgeConfig
from src.weather_edge.models import Bin
from src.weather_edge.scanner import assess_side, confidence_label, run_scan, side_spread
from src.weather_edge.calibration import Calibration
from tests.weather_edge_helpers import (
    NOW, FakeForecasts, FakeKalshi, FakePoly, book, bundle, load, one_event_contract, quote, seed_calibration,
)

CHEAP_YES = book(yes_bids=[("0.15", 500)], no_bids=[("0.80", 500)])      # YES ask 0.20, bid 0.15


def cfg(**kw):
    base = dict(max_quote_age_seconds=300, min_net_ev_pct=5.0)
    base.update(kw)
    return WeatherEdgeConfig(**base)


def contract(**kw):
    kw.setdefault("quote", quote(yes_asks=[("0.20", 500)], no_asks=[("0.85", 500)], yes_bid="0.15", no_bid="0.80", depth=False))
    return one_event_contract(**kw)


def scan(conn, contracts, *, forecasts=None, kalshi_book=CHEAP_YES, polymarket=None, config=None, now=NOW, sender=None, **kk):
    return run_scan(conn, config or cfg(), now=now, kalshi=FakeKalshi(contracts, kalshi_book, **kk),
                    polymarket=polymarket or FakePoly(), forecasts=forecasts or FakeForecasts(73.0), sender=sender)


@pytest.fixture
def calibrated(db_conn):
    seed_calibration(db_conn, n=80, std=2.0)
    return db_conn


def signals(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM weather_signals ORDER BY signal_id")]


class TestHappyPath:
    def test_underpriced_yes_becomes_a_signal_with_paper_trade(self, calibrated):
        s = scan(calibrated, [contract()])
        assert s.status == "ok" and s.signals == 1 and s.paper_trades == 1 and s.predictions == 1
        sig = [x for x in signals(calibrated) if x["decision"] == "SIGNAL"][0]
        assert sig["side"] == "YES" and sig["net_ev_pct"] > 5 and sig["conservative_net_ev"] > 0
        assert sig["break_even_prob"] < sig["p_win"] and sig["fee"] > 0 and sig["avg_price"] == pytest.approx(0.20)
        t = calibrated.execute("SELECT * FROM weather_paper_trades").fetchone()
        assert t["is_paper"] == 1 and t["status"] == "open" and t["total_cost"] == pytest.approx(sig["total_cost"])

    def test_prediction_is_recorded_before_any_outcome_exists(self, calibrated):
        scan(calibrated, [contract()])
        p = calibrated.execute("SELECT * FROM weather_predictions").fetchone()
        assert p["outcome_yes"] is None and p["resolved_at"] is None and p["model_version"]
        assert p["forecast_snapshot_id"] and p["market_snapshot_id"] and p["calibrated"] == 1
        assert 0.01 <= p["p_yes_low"] <= p["p_yes"] <= p["p_yes_high"] <= 0.99
        assert p["baseline_p_yes"] is not None and p["market_yes_mid"] == pytest.approx(0.175)

    def test_no_side_signal_when_the_market_overprices_yes(self, calibrated):
        rich = book(yes_bids=[("0.85", 500)], no_bids=[("0.10", 500)])     # YES bid .85 / ask .90; NO ask .15
        c = contract(quote=quote(yes_asks=[("0.90", 500)], no_asks=[("0.15", 500)], yes_bid="0.85", no_bid="0.10", depth=False),
                     cfg_bin_lo=80, cfg_bin_hi=81)
        s = scan(calibrated, [c], kalshi_book=rich)
        sig = [x for x in signals(calibrated) if x["decision"] == "SIGNAL"]
        assert s.signals == 1 and sig[0]["side"] == "NO" and sig[0]["p_win"] > 0.9

    def test_scan_run_is_recorded_with_census(self, calibrated):
        s = scan(calibrated, [contract()])
        row = store.latest_scan(calibrated)
        assert row["scan_id"] == s.scan_id and row["status"] == "ok" and row["signals_created"] == 1


class TestIdempotency:
    def test_rerunning_the_same_scan_creates_no_duplicates(self, calibrated):
        scan(calibrated, [contract()])
        counts = lambda: [calibrated.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
                          for t in ("weather_predictions", "weather_signals", "weather_paper_trades", "weather_market_snapshots")]
        first = counts()
        s2 = scan(calibrated, [contract()])
        assert counts() == first and s2.signals == 0 and s2.predictions == 0

    def test_a_later_scan_does_not_stack_a_second_paper_trade_on_the_same_contract(self, calibrated):
        scan(calibrated, [contract()])
        later = NOW + timedelta(minutes=45)
        s2 = scan(calibrated, [contract(quote=quote(yes_asks=[("0.20", 500)], no_asks=[("0.85", 500)], yes_bid="0.15", no_bid="0.80", depth=False, at=later))],
                  now=later, forecasts=FakeForecasts(73.0, fetched_at=later))
        assert s2.signals == 0
        assert calibrated.execute("SELECT COUNT(*) AS n FROM weather_paper_trades").fetchone()["n"] == 1
        assert "ALREADY_OPEN" in (signals(calibrated)[-1]["reasons"] or "")


class TestGates:
    def test_stale_forecast_blocks_signals(self, calibrated):
        old = NOW - timedelta(hours=5)
        s = scan(calibrated, [contract()], forecasts=FakeForecasts(73.0, fetched_at=old))
        assert s.signals == 0 and s.reject_reasons.get("STALE_FORECAST")

    def test_uncalibrated_station_blocks_signals_by_default(self, db_conn):
        s = scan(db_conn, [contract()])
        assert s.signals == 0 and s.reject_reasons.get("UNCALIBRATED")
        p = db_conn.execute("SELECT calibrated FROM weather_predictions").fetchone()
        assert p["calibrated"] == 0                                    # still recorded, flagged

    def test_calibration_requirement_can_be_relaxed_explicitly(self, db_conn):
        s = scan(db_conn, [contract()], config=cfg(require_calibration=False))
        assert s.signals == 1

    def test_polymarket_is_never_calibrated_from_kalshi_history(self, calibrated):
        poly_c = one_event_contract(
            platform="polymarket", market_id="P-1", event_key="PE-1", fee_model="polymarket_curve", fee_param=D("0.05"),
            min_order=D(5), yes_token="y", no_token="n", settlement_source="NOAA (weather.gov observations)",
            quote=quote(yes_asks=[("0.20", 900)], no_asks=[("0.85", 900)], yes_bid="0.15", no_bid="0.80", depth=False))
        yes_b = {"asks": [{"price": "0.20", "size": "900"}], "bids": [{"price": "0.15", "size": "900"}]}
        no_b = {"asks": [{"price": "0.85", "size": "900"}], "bids": [{"price": "0.80", "size": "900"}]}
        s = scan(calibrated, [], polymarket=FakePoly([poly_c], books={"y": yes_b, "n": no_b}))
        assert s.signals == 0 and s.reject_reasons.get("UNCALIBRATED")
        p = calibrated.execute("SELECT calibrated FROM weather_predictions WHERE platform = 'polymarket'").fetchone()
        assert p["calibrated"] == 0

    def test_closed_market_is_excluded_from_the_scan(self, calibrated):
        s = scan(calibrated, [contract(status="closed")])
        assert s.contracts_modeled == 0 and s.predictions == 0 and s.signals == 0

    def test_unmodeled_contracts_are_stored_with_a_reason_and_never_predicted(self, calibrated):
        c = contract(modeled=False, unmodeled_reason="rain markets are discovered but not modeled yet")
        s = scan(calibrated, [c])
        assert s.contracts_seen == 1 and s.predictions == 0
        row = store.get_contract(calibrated, "kalshi", "T-1")
        assert row["modeled"] == 0 and "rain" in row["unmodeled_reason"]

    def test_target_date_in_the_past_or_beyond_horizon_is_skipped(self, calibrated):
        s = scan(calibrated, [contract(target_date=date(2026, 10, 10))])
        assert s.predictions == 0 and "already passed" in list(s.skipped_events.values())[0]
        s = scan(calibrated, [contract(target_date=date(2026, 10, 20))])
        assert "beyond max" in list(s.skipped_events.values())[0]

    def test_same_day_market_without_observations_is_skipped(self, calibrated):
        s = scan(calibrated, [contract()], forecasts=FakeForecasts(73.0, same_day=True, observed=None))
        assert s.predictions == 0 and "same-day" in list(s.skipped_events.values())[0]

    def test_same_day_observed_high_changes_the_probabilities(self, calibrated):
        scan(calibrated, [contract(cfg_bin_lo=None, cfg_bin_hi=72)], forecasts=FakeForecasts(73.0, same_day=True, observed=78.0),
             kalshi_book=CHEAP_YES)
        p = calibrated.execute("SELECT p_yes FROM weather_predictions").fetchone()["p_yes"]
        assert p <= 0.02       # a high of 72 or below is essentially ruled out once 78 has been observed

    def test_event_exposure_cap(self, calibrated):
        cs = [contract(market_id=f"T-{i}", cfg_bin_lo=70 + 2 * i, cfg_bin_hi=71 + 2 * i) for i in range(3)]
        s = scan(calibrated, cs, config=cfg(max_event_exposure_usd=30.0, paper_stake_usd=25.0))
        assert s.paper_trades == 1 and s.reject_reasons.get("EVENT_CAP", 0) >= 1
        capped = [x for x in signals(calibrated) if "EVENT_CAP" in (x["reasons"] or "")]
        assert capped and all(x["decision"] == "REJECTED" for x in capped)

    @pytest.mark.parametrize("kw,reason", [
        (dict(max_spread=0.01), "WIDE_SPREAD"),
        (dict(min_depth_usd=1e6), "THIN_DEPTH"),
        (dict(min_entry_price=0.5), "PRICE_OUT_OF_RANGE"),
        (dict(min_net_ev_pct=1e6), "BELOW_MIN_EDGE"),
    ])
    def test_each_safety_gate_rejects_and_records_why(self, calibrated, kw, reason):
        s = scan(calibrated, [contract()], config=cfg(**kw))
        assert s.signals == 0 and s.reject_reasons.get(reason)
        row = [x for x in signals(calibrated)][0]
        assert row["decision"] == "REJECTED" and reason in row["reasons"]
        assert calibrated.execute("SELECT COUNT(*) AS n FROM weather_paper_trades").fetchone()["n"] == 0


class TestAssessSide:
    C = staticmethod(lambda **k: contract(**k))

    def assess(self, c, q, side="YES", p=0.5, lo=0.45, hi=0.55, **kw):
        return assess_side(c, q, side, p, lo, hi, cfg(**kw.pop("config", {})), NOW, calibrated=kw.pop("calibrated", True),
                           forecast_fresh=kw.pop("fresh", True))

    def test_clean_edge_passes(self):
        a = self.assess(self.C(), quote(yes_asks=[("0.30", 500)], no_asks=[("0.75", 500)], yes_bid="0.26", no_bid="0.70"), p=0.55, lo=0.50, hi=0.60)
        assert a.passes and a.ev.net_ev_pct > 5

    def test_stale_quote_and_closed_and_expired_close_time(self):
        old = quote(yes_asks=[("0.30", 500)], yes_bid="0.26", at=NOW - timedelta(hours=1))
        assert "STALE_QUOTE" in self.assess(self.C(), old, p=0.6).reasons
        fresh = quote(yes_asks=[("0.30", 500)], yes_bid="0.26")
        assert "CLOSED" in self.assess(self.C(status="closed"), fresh, p=0.6).reasons
        assert "CLOSED" in self.assess(self.C(close_time=NOW - timedelta(minutes=1)), fresh, p=0.6).reasons

    def test_missing_depth_or_asks_or_bid(self):
        assert "DEPTH_UNKNOWN" in self.assess(self.C(), quote(yes_asks=[("0.30", 500)], yes_bid="0.26", depth=False), p=0.6).reasons
        assert "NO_ASK" in self.assess(self.C(), quote(no_asks=[("0.75", 500)]), p=0.6).reasons
        assert "NO_BID" in self.assess(self.C(), quote(yes_asks=[("0.30", 500)], yes_bid=None), p=0.6).reasons

    def test_partial_fill_is_flagged(self):
        a = self.assess(self.C(), quote(yes_asks=[("0.30", 5)], yes_bid="0.26"), p=0.6, lo=0.55)
        assert "PARTIAL_FILL" in a.reasons

    def test_nonrobust_edge_is_rejected_even_when_point_ev_is_positive(self):
        a = self.assess(self.C(), quote(yes_asks=[("0.40", 500)], yes_bid="0.37"), p=0.50, lo=0.30, hi=0.60,
                        config=dict(min_net_ev_pct=1.0))
        assert a.ev.net_ev_pct > 1 and "NONROBUST_EV" in a.reasons

    def test_no_side_uses_complement_and_its_own_book(self):
        a = self.assess(self.C(), quote(yes_asks=[("0.90", 500)], no_asks=[("0.20", 500)], yes_bid="0.85", no_bid="0.10"),
                        side="NO", p=0.1, lo=0.08, hi=0.15)
        assert a.p_win == pytest.approx(0.9) and a.p_win_robust == pytest.approx(0.85) and a.ev.avg_price == D("0.20")

    def test_side_spread_derives_no_bid_when_missing(self):
        q = quote(yes_asks=[("0.30", 5)], no_asks=[("0.74", 5)], yes_bid="0.26")
        assert side_spread(q, "YES") == D("0.04") and side_spread(q, "NO") == D("0.74") - D("0.70")

    def test_confidence_label_rules(self):
        good = Calibration(0, 2.0, 0.2, 150, True, "x")
        a = self.assess(self.C(), quote(yes_asks=[("0.30", 500)], yes_bid="0.26"), p=0.6, lo=0.55, hi=0.65)
        assert confidence_label(good, bundle(73.0), 0, a) == "high"
        assert confidence_label(Calibration(0, 2.0, 0.2, 40, True, "x"), bundle(73.0), 2, a) == "medium"
        assert confidence_label(good, bundle(73.0, nws=80.0), 0, a) == "low"        # sources disagree
        assert confidence_label(Calibration(0, 2.0, 0.2, 0, False, "x"), bundle(73.0), 0, a) == "low"


class TestFailureHandling:
    def test_total_outage_is_recorded_as_failed_and_the_scan_survives(self, calibrated):
        s = scan(calibrated, [], fail_discover=True, polymarket=FakePoly(fail_discover=True))
        assert s.status == "failed" and any("kalshi discovery failed" in e for e in s.errors)
        assert store.latest_scan(calibrated)["status"] == "failed"

    def test_kalshi_outage_alone_is_partial(self, calibrated):
        s = scan(calibrated, [], fail_discover=True)
        assert s.status == "partial"

    def test_one_source_down_is_partial_not_failed(self, calibrated):
        s = run_scan(calibrated, cfg(), now=NOW, kalshi=FakeKalshi([contract()], CHEAP_YES),
                     polymarket=FakePoly(fail_discover=True), forecasts=FakeForecasts(73.0))
        assert s.status == "partial" and s.signals == 1

    def test_forecast_outage_skips_events_without_guessing(self, calibrated):
        s = scan(calibrated, [contract()], forecasts=FakeForecasts(fail=True))
        assert s.predictions == 0 and s.signals == 0 and "simulated outage" in list(s.skipped_events.values())[0]

    def test_order_book_failure_records_error_and_no_signal(self, calibrated):
        s = scan(calibrated, [contract()], fail_book=True)
        assert s.signals == 0 and any("order book" in e for e in s.errors) and s.status == "partial"

    def test_corrupt_forecast_does_not_crash_the_scan(self, calibrated):
        class Boom:
            def bundle(self, *a):
                raise KeyError("weird")
        s = scan(calibrated, [contract()], forecasts=Boom())
        assert s.signals == 0 and "forecast error" in list(s.skipped_events.values())[0]


class TestAlerts:
    URL = "https://discord.example/webhook/abc"

    def run(self, conn, sender, now=NOW, **cfgkw):
        return scan(conn, [contract(quote=quote(yes_asks=[("0.20", 500)], no_asks=[("0.85", 500)], yes_bid="0.15", no_bid="0.80", depth=False, at=now))],
                    now=now, forecasts=FakeForecasts(73.0, fetched_at=now), sender=sender,
                    config=cfg(discord_webhook_url=self.URL, alert_min_net_ev_pct=5.0, **cfgkw))

    def test_alert_sent_once_and_not_repeated_by_a_later_scan_the_same_day(self, calibrated):
        sent = []
        s = self.run(calibrated, lambda url, text: sent.append((url, text)) or True)
        assert s.alerts["sent"] == 1 and sent[0][0] == self.URL
        assert "paper-only" in sent[0][1] and "Net EV" in sent[0][1]
        # same signal re-detected later the same day: no new signal (ALREADY_OPEN) and no new alert
        later = NOW + timedelta(minutes=45)
        self.run(calibrated, lambda url, text: sent.append((url, text)) or True, now=later)
        assert len(sent) == 1

    def test_failed_send_is_released_for_retry(self, calibrated):
        s = self.run(calibrated, lambda url, text: False)
        assert s.alerts["failed"] == 1
        assert not store.alert_sent(calibrated, f"kalshi|T-1|YES|{NOW.date().isoformat()}")

    def test_sender_exception_never_breaks_the_scan(self, calibrated):
        def boom(url, text):
            raise RuntimeError("discord down")
        s = self.run(calibrated, boom)
        assert s.status == "ok" and s.alerts["failed"] == 1

    def test_no_webhook_means_no_alert(self, calibrated):
        s = scan(calibrated, [contract()], sender=lambda *a: pytest.fail("must not send"))
        assert s.signals == 1 and s.alerts.get("disabled")

    def test_watchlist_restricts_alerts_to_watched_cities(self, calibrated):
        store.watch(calibrated, "KLGA", "high", 5.0, NOW)
        sent = []
        s = self.run(calibrated, lambda u, t: sent.append(t) or True)
        assert s.signals == 1 and not sent and s.alerts["skipped_filter"] == 1

    def test_alert_threshold_filters_small_edges(self, calibrated):
        sent = []
        self.run(calibrated, lambda u, t: sent.append(t) or True)
        assert sent  # sanity: large edge alerts
        item = {"platform": "kalshi", "market_id": "Z", "side": "YES", "station_icao": "KNYC", "metric": "high", "net_ev_pct": 3.0}
        assert not alerts._eligible(item, cfg(alert_min_net_ev_pct=8.0), [])


def test_alert_text_cannot_ping_anyone():
    item = {"platform": "kalshi", "station_label": "X @everyone", "metric": "high", "target_date": "2026-10-12",
            "side": "YES", "bin_label": "72° to 73°", "avg_price": 0.2, "p_win": 0.4, "break_even_prob": 0.21,
            "net_ev_pct": 50.0, "net_ev": 0.1, "confidence": "low"}
    assert "@" not in alerts.format_alert(item)


def test_celsius_contracts_are_counted_not_stored(calibrated):
    c = contract(modeled=False, unmodeled_reason="Celsius market (only °F is modeled)", market_id="C-1")
    s = scan(calibrated, [c, contract()])
    assert s.census["celsius_skipped"] == 1
    assert store.get_contract(calibrated, "kalshi", "C-1") is None and store.get_contract(calibrated, "kalshi", "T-1")
