"""Weather Edge: forecast aggregation, cross-platform matching, calibration, persistence idempotency."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from src.weather_edge import calibration as cal
from src.weather_edge import forecast as fc
from src.weather_edge import kalshi_source as ks
from src.weather_edge import matching, polymarket_source as pm, store
from src.weather_edge.http import ProviderError
from src.weather_edge.registry import STATIONS, station_by_icao, station_for_cli
from tests.weather_edge_helpers import (
    NOW, bundle, kalshi_contracts, kalshi_markets, kalshi_series, load, one_event_contract, seed_calibration,
    synthetic_hourly,
)


class TestRegistry:
    def test_cli_codes_map_to_stations_with_the_nyc_exception(self):
        assert station_for_cli("CLINYC").icao == "KNYC"
        assert station_for_cli("CLIMDW").icao == "KMDW" and station_for_cli("CLILAX").icao == "KLAX"
        assert station_for_cli("CLIZZZ") is None and station_for_cli(None) is None
        assert station_by_icao("KLGA").tz == "America/New_York" and station_by_icao("NOPE") is None

    def test_every_station_has_coordinates_and_a_valid_timezone(self):
        from zoneinfo import ZoneInfo
        for s in STATIONS.values():
            assert -90 < s.lat < 90 and -180 < s.lon < 0 and ZoneInfo(s.tz)


class TestForecastAggregation:
    def test_per_member_daily_high_over_the_local_day(self):
        h = synthetic_hourly(date(2026, 10, 12), 3, centre=73.0, spread=1.0)
        sets = fc.daily_extremes_by_member(h, date(2026, 10, 13), "high")
        assert {s.family for s in sets} == {"gefs", "ecmwf"}
        counts = {s.family: len(s.values) for s in sets}
        assert counts == {"gefs": 31, "ecmwf": 51}
        allv = [v for s in sets for v in s.values]
        assert 71 < sum(allv) / len(allv) < 75

    def test_low_is_the_minimum_and_lower_than_the_high(self):
        h = synthetic_hourly(date(2026, 10, 12), 1, centre=73.0, spread=1.0)
        hi = fc.daily_extremes_by_member(h, date(2026, 10, 12), "high")[0].values[0]
        lo = fc.daily_extremes_by_member(h, date(2026, 10, 12), "low")[0].values[0]
        assert hi - lo == pytest.approx(16.0, abs=0.1)

    def test_partial_days_are_dropped_not_extrapolated(self):
        h = synthetic_hourly(date(2026, 10, 12), 1, centre=73.0, spread=1.0)
        for col in h:
            if col != "time":
                h[col] = h[col][:10]           # only 10 hours present
        assert fc.daily_extremes_by_member(h, date(2026, 10, 12), "high") == []

    def test_null_values_are_skipped_and_a_dead_column_ignored(self):
        h = synthetic_hourly(date(2026, 10, 12), 1, centre=73.0, spread=1.0, n_gefs=3, n_ecmwf=3)
        first = next(c for c in h if c != "time")
        h[first] = [None] * 24
        sets = fc.daily_extremes_by_member(h, date(2026, 10, 12), "high")
        assert sum(len(s.values) for s in sets) == 5

    def test_unsupported_metric_raises(self):
        with pytest.raises(ValueError):
            fc.daily_extremes_by_member({"time": []}, date(2026, 10, 12), "rain")

    def test_real_fixture_columns_are_recognised(self):
        e = load("openmeteo_ensemble.json")
        sets = fc.daily_extremes_by_member(e["hourly"], date(2026, 10, 12), "high")
        assert sets and all(len(s.values) > 3 for s in sets)

    def test_nws_extreme_uses_the_stations_local_day(self):
        periods = load("nws_hourly.json")["properties"]["periods"]
        v = fc.nws_daily_extreme(periods, date(2026, 10, 12), "America/New_York", "high")
        assert v is not None and 55 < v < 90
        assert fc.nws_daily_extreme(periods, date(2026, 10, 30), "America/New_York", "high") is None

    def test_observed_extreme_converts_celsius_and_filters_to_the_local_date(self):
        feats = [
            {"properties": {"timestamp": "2026-10-12T14:00:00+00:00", "temperature": {"value": 20.0, "unitCode": "wmoUnit:degC"}}},
            {"properties": {"timestamp": "2026-10-12T16:00:00+00:00", "temperature": {"value": 25.0, "unitCode": "wmoUnit:degC"}}},
            {"properties": {"timestamp": "2026-10-11T16:00:00+00:00", "temperature": {"value": 30.0, "unitCode": "wmoUnit:degC"}}},
            {"properties": {"timestamp": "2026-10-12T17:00:00+00:00", "temperature": {"value": None, "unitCode": "wmoUnit:degC"}}},
        ]
        assert fc.observed_extreme(feats, date(2026, 10, 12), "America/New_York", "high") == pytest.approx(77.0)
        assert fc.observed_extreme(feats, date(2026, 10, 12), "America/New_York", "low") == pytest.approx(68.0)
        assert fc.observed_extreme([], date(2026, 10, 12), "America/New_York", "high") is None

    def test_lead_days_and_buckets_use_local_dates(self):
        late_utc = datetime(2026, 10, 13, 2, 0, tzinfo=timezone.utc)      # still Oct 12 in New York
        assert fc.lead_days(date(2026, 10, 12), late_utc, "America/New_York") == 0
        assert [fc.lead_bucket(n) for n in (0, 1, 2, 5)] == ["d0", "d1", "d2", "d3+"]


class _Http:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get_json(self, path, params=None):
        self.calls.append(path)
        for key, val in self.routes.items():
            if path.startswith(key):
                if isinstance(val, Exception):
                    raise val
                return val
        raise ProviderError("x", "no route", 404)


class TestForecastService:
    def service(self, ens, nws=None, now=NOW):
        pts = {"properties": {"forecastHourly": "https://api.weather.gov/gridpoints/X/1,1/forecast/hourly"}}
        hourly = {"properties": {"periods": load("nws_hourly.json")["properties"]["periods"]}}
        routes = {"/points": pts, "https://api.weather.gov/gridpoints": hourly,
                  "/stations": {"features": load("nws_observations.json")["features"]}}
        routes.update(nws or {})
        return fc.ForecastService(ensemble_http=_Http({"/v1/ensemble": ens}), nws_http=_Http(routes), clock=lambda: now)

    def test_bundle_combines_ensemble_and_nws_and_caches_per_station(self):
        ens = {"hourly": synthetic_hourly(date(2026, 10, 12), 4, 73.0, 1.5)}
        svc = self.service(ens)
        b = svc.bundle(STATIONS["KNYC"], "high", date(2026, 10, 13))
        assert len(b.all_values) == 82 and b.nws_point is not None and not b.same_day
        svc.bundle(STATIONS["KNYC"], "high", date(2026, 10, 14))
        assert svc.ensemble_http.calls.count("/v1/ensemble") == 1

    def test_same_day_bundle_pulls_observations(self):
        ens = {"hourly": synthetic_hourly(date(2026, 10, 12), 2, 73.0, 1.5)}
        b = self.service(ens).bundle(STATIONS["KNYC"], "high", date(2026, 10, 12))
        assert b.same_day

    def test_ensemble_outage_raises_forecast_unavailable(self):
        svc = self.service(ProviderError("open-meteo", "HTTP 500", 500))
        with pytest.raises(fc.ForecastUnavailable):
            svc.bundle(STATIONS["KNYC"], "high", date(2026, 10, 13))

    def test_too_few_members_is_unavailable(self):
        ens = {"hourly": synthetic_hourly(date(2026, 10, 12), 3, 73.0, 1.5, n_gefs=4, n_ecmwf=4)}
        with pytest.raises(fc.ForecastUnavailable, match="only 8"):
            self.service(ens).bundle(STATIONS["KNYC"], "high", date(2026, 10, 13))

    def test_nws_failure_degrades_but_does_not_invalidate(self):
        ens = {"hourly": synthetic_hourly(date(2026, 10, 12), 3, 73.0, 1.5)}
        b = self.service(ens, nws={"/points": ProviderError("nws", "HTTP 503", 503)}).bundle(STATIONS["KNYC"], "high", date(2026, 10, 13))
        assert b.nws_point is None and "NWS forecast unavailable" in b.source_note

    def test_past_dates_are_refused(self):
        ens = {"hourly": synthetic_hourly(date(2026, 10, 12), 3, 73.0, 1.5)}
        with pytest.raises(fc.ForecastUnavailable):
            self.service(ens).bundle(STATIONS["KNYC"], "high", date(2026, 10, 10))


class TestMatching:
    def poly(self):
        return pm.parse_event(load("polymarket_event_nyc_high.json"), NOW)

    def test_kalshi_and_polymarket_nyc_are_not_equivalent_and_say_why(self):
        k = ks.parse_market(kalshi_markets()[0], kalshi_series(), NOW)
        p = self.poly()[0]
        r = matching.check_equivalence(k, p)
        assert not r.equivalent
        assert any("stations" in x for x in r.reasons) and any("sources" in x for x in r.reasons)

    def test_identical_definitions_are_equivalent(self):
        a = one_event_contract(platform="kalshi")
        b = one_event_contract(platform="polymarket", market_id="P-1")
        assert matching.check_equivalence(a, b).equivalent

    @pytest.mark.parametrize("field,value,needle", [
        ("target_date", date(2026, 10, 13), "date"),
        ("metric", "low", "measurement"),
        ("settlement_source", "NOAA (weather.gov)", "sources"),
        ("tz", "America/Chicago", "time zones"),
        ("unit", "C", "units"),
    ])
    def test_any_single_mismatch_breaks_equivalence(self, field, value, needle):
        a = one_event_contract(platform="kalshi")
        b = one_event_contract(platform="polymarket", market_id="P-1", **{field: value})
        r = matching.check_equivalence(a, b)
        assert not r.equivalent and any(needle in x for x in r.reasons)

    def test_different_bin_edges_and_unknown_sources_are_not_equivalent(self):
        a = one_event_contract(platform="kalshi")
        assert not matching.check_equivalence(a, one_event_contract(platform="polymarket", cfg_bin_lo=72, cfg_bin_hi=74)).equivalent
        assert not matching.check_equivalence(a, one_event_contract(platform="polymarket", settlement_source="???")).equivalent
        assert not matching.check_equivalence(a, one_event_contract(platform="polymarket", station_icao=None)).equivalent

    def test_pair_search_only_considers_same_metric_date_station(self):
        a = one_event_contract(platform="kalshi")
        b = one_event_contract(platform="polymarket", market_id="P-1", station_icao="KLGA")
        c = one_event_contract(platform="polymarket", market_id="P-2")
        pairs = matching.find_cross_platform_pairs([a, b, c])
        assert len(pairs) == 1 and pairs[0].equivalent

    def test_source_families(self):
        assert matching.source_family("The Weather Company") == "twc"
        assert matching.source_family("NOAA (weather.gov observations)") == "noaa"
        assert matching.source_family("") == "unknown"


class TestCalibration:
    def test_no_samples_is_prior_and_uncalibrated(self):
        c = cal.fit_calibration([], "d1", "x")
        assert not c.calibrated and c.n == 0 and c.residual_std == cal.PRIOR_STD_F["d1"]

    def test_few_samples_are_uncalibrated_and_shrunk(self):
        c = cal.fit_calibration([3.0] * 5, "d1", "x")
        assert not c.calibrated and 0 < c.bias < 3.0

    def test_enough_samples_recover_bias_and_spread(self):
        import random
        r = random.Random(1)
        errs = [1.5 + r.gauss(0, 2.0) for _ in range(400)]
        c = cal.fit_calibration(errs, "d1", "x")
        assert c.calibrated and c.bias == pytest.approx(1.5, abs=0.3) and c.residual_std == pytest.approx(2.0, abs=0.3)
        assert c.bias_se < 0.2

    def test_scoring_helpers(self):
        assert cal.brier(0.8, True) == pytest.approx(0.04) and cal.brier(0.8, False) == pytest.approx(0.64)
        assert cal.log_loss(0.5, True) == pytest.approx(0.6931, abs=1e-3)
        assert cal.log_loss(0.0, True) < 20       # clipped, never infinite

    def test_lookup_never_transfers_between_platforms(self, db_conn):
        seed_calibration(db_conn, platform="kalshi", bucket="d1", n=80, bias=1.0)
        assert cal.lookup(db_conn, "kalshi", "KNYC", "high", "d1").calibrated
        other = cal.lookup(db_conn, "polymarket", "KNYC", "high", "d1")
        assert not other.calibrated and other.n == 0

    def test_collect_errors_pairs_each_settled_day_with_its_archived_forecast(self):
        h = {"time": [f"2026-10-0{d}T{h:02d}:00" for d in (1, 2) for h in range(24)],
             "temperature_2m_previous_day1": [60 + 10 * (h == 15) for d in (1, 2) for h in range(24)],
             "temperature_2m_previous_day2": [58 + 10 * (h == 15) for d in (1, 2) for h in range(24)]}
        errs = cal.collect_errors({("KNYC", "high", date(2026, 10, 1)): 72.0}, {"KNYC": {"hourly": h}}, STATIONS)
        assert errs[("KNYC", "high", "d1")] == [2.0] and errs[("KNYC", "high", "d2")] == [4.0]

    def test_backfill_fits_from_kalshi_settled_markets_and_reports_thin_buckets(self, db_conn):
        class K:
            def list_weather_series(self_):
                return [kalshi_series()]

            def list_markets(self_, ticker, status="open"):
                return kalshi_markets("kalshi_markets_KXHIGHNY_settled.json")

        days = sorted({m["ticker"].split("-")[1] for m in kalshi_markets("kalshi_markets_KXHIGHNY_settled.json")})
        assert days

        class P:
            def get_json(self_, path, params=None):
                start = date.fromisoformat(params["start_date"])
                end = date.fromisoformat(params["end_date"])
                times, d1, d2 = [], [], []
                cur = start
                while cur <= end:
                    for hh in range(24):
                        times.append(f"{cur.isoformat()}T{hh:02d}:00")
                        d1.append(70.0 if hh == 15 else 55.0)
                        d2.append(69.0 if hh == 15 else 55.0)
                    cur += timedelta(days=1)
                return {"hourly": {"time": times, "temperature_2m_previous_day1": d1, "temperature_2m_previous_day2": d2}}

        report = cal.backfill(db_conn, K(), STATIONS, previous_http=P(), now=NOW + timedelta(days=30), lookback_days=365)
        assert report["settled_days"] >= 1 and report["fitted"] >= 1
        row = store.get_calibration(db_conn, "kalshi", "KNYC", "high", "d1")
        assert row and row["n"] == report["settled_days"]
        assert any("samples" in s for s in report["skipped"])         # too few days -> flagged, not trusted

    def test_backfill_with_no_settled_markets_reports_it(self, db_conn):
        class K:
            def list_weather_series(self_):
                return [kalshi_series()]

            def list_markets(self_, ticker, status="open"):
                return []
        out = cal.backfill(db_conn, K(), STATIONS, previous_http=object(), now=NOW)
        assert out["fitted"] == 0 and out["skipped"]


class TestStoreIdempotency:
    def test_prediction_signal_and_trade_are_not_duplicated(self, db_conn):
        pred = {"prediction_key": "k1", "platform": "kalshi", "market_id": "M", "created_at": NOW.isoformat(),
                "model_version": "v", "lead_days": 0, "p_yes": 0.4}
        a, created = store.insert_prediction(db_conn, pred)
        b, created2 = store.insert_prediction(db_conn, pred)
        assert created and not created2 and a == b
        sig = {"signal_key": "s1", "prediction_id": a, "platform": "kalshi", "market_id": "M", "created_at": NOW.isoformat(),
               "side": "YES", "decision": "SIGNAL"}
        s1, c1 = store.insert_signal(db_conn, sig)
        s2, c2 = store.insert_signal(db_conn, sig)
        assert c1 and not c2 and s1 == s2
        t = {"signal_id": s1, "platform": "kalshi", "market_id": "M", "side": "YES", "qty": 10, "total_cost": 4.0,
             "p_win": 0.5, "opened_at": NOW.isoformat()}
        assert store.insert_paper_trade(db_conn, t)[1] and not store.insert_paper_trade(db_conn, t)[1]
        n = db_conn.execute("SELECT COUNT(*) AS n FROM weather_paper_trades").fetchone()["n"]
        assert n == 1

    def test_snapshots_are_idempotent(self, db_conn):
        b = bundle(73.0)
        assert store.insert_forecast_snapshot(db_conn, b, 0) == store.insert_forecast_snapshot(db_conn, b, 0)
        c = kalshi_contracts()[0]
        assert store.insert_market_snapshot(db_conn, c, c.quote, NOW) == store.insert_market_snapshot(db_conn, c, c.quote, NOW)

    def test_contract_upsert_keeps_first_seen_and_updates_status(self, db_conn):
        c = kalshi_contracts()[0]
        store.upsert_contract(db_conn, c, NOW)
        store.upsert_contract(db_conn, c, NOW + timedelta(hours=1))
        row = store.get_contract(db_conn, c.platform, c.market_id)
        assert row["first_seen_at"] != row["last_seen_at"] and row["modeled"] == 1
        assert db_conn.execute("SELECT COUNT(*) AS n FROM weather_contracts").fetchone()["n"] == 1

    def test_alert_log_is_at_most_once_and_can_be_released(self, db_conn):
        assert store.mark_alert_sent(db_conn, "a", 1, "discord", NOW)
        assert not store.mark_alert_sent(db_conn, "a", 1, "discord", NOW)
        store.unmark_alert_sent(db_conn, "a")
        assert store.mark_alert_sent(db_conn, "a", 1, "discord", NOW)

    def test_watchlist_upsert_and_remove(self, db_conn):
        store.watch(db_conn, "KNYC", "high", 10.0, NOW)
        store.watch(db_conn, "KNYC", "high", 12.0, NOW)
        rows = store.watchlist(db_conn)
        assert len(rows) == 1 and rows[0]["min_net_ev_pct"] == 12.0
        store.unwatch(db_conn, "KNYC", "high")
        assert store.watchlist(db_conn) == []

    def test_schema_creation_is_idempotent(self, db_conn):
        store.create_schema(db_conn)
        store.create_schema(db_conn)
