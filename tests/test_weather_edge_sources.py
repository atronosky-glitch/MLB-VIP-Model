"""Weather Edge: provider parsers (real captured payloads), order-book handling, HTTP safety."""

import copy
from datetime import date, datetime, timezone
from decimal import Decimal as D

import pytest
import requests

from src.weather_edge import kalshi_source as ks
from src.weather_edge import polymarket_source as pm
from src.weather_edge.http import ProviderError, PublicHttpClient
from src.weather_edge.models import Bin
from tests.weather_edge_helpers import NOW, kalshi_markets, kalshi_series, load


class TestKalshiParser:
    def test_every_open_market_parses_to_a_verified_station_and_bin(self):
        cs = [ks.parse_market(m, kalshi_series(), NOW) for m in kalshi_markets()]
        assert cs and all(c.modeled for c in cs), [c.unmodeled_reason for c in cs if not c.modeled]
        c = cs[0]
        assert c.platform == "kalshi" and c.station_icao == "KNYC" and c.metric == "high"
        assert c.settlement_source == "The Weather Company"
        assert c.fee_model == "kalshi_quadratic" and c.fee_param == D("1")

    def test_strike_bins_match_the_human_labels(self):
        by_id = {c.market_id: c for c in (ks.parse_market(m, kalshi_series(), NOW) for m in kalshi_markets())}
        assert by_id["KXHIGHNY-26OCT12-T76"].bin == Bin(77, None)          # ">76" on whole degrees
        assert by_id["KXHIGHNY-26OCT12-T69"].bin == Bin(None, 68)          # "<69"
        assert by_id["KXHIGHNY-26OCT12-B75.5"].bin == Bin(75, 76)

    def test_a_day_of_markets_forms_one_complete_partition(self):
        from src.weather_edge.probability import check_partition
        cs = [ks.parse_market(m, kalshi_series(), NOW) for m in kalshi_markets()]
        day = [c.bin for c in cs if c.target_date == date(2026, 10, 12)]
        ok, problems = check_partition(day)
        assert ok, problems

    def test_label_that_disagrees_with_strikes_is_not_modeled(self):
        m = copy.deepcopy(kalshi_markets()[0])
        m["yes_sub_title"] = "50° to 51°"
        c = ks.parse_market(m, kalshi_series(), NOW)
        assert not c.modeled and "disagree" in c.unmodeled_reason

    def test_unknown_station_is_not_modeled(self):
        m = copy.deepcopy(kalshi_markets()[0])
        m["rules_primary"] = m["rules_primary"].replace("CLINYC", "CLIZZZ")
        c = ks.parse_market(m, kalshi_series(), NOW)
        assert not c.modeled and "not in the verified registry" in c.unmodeled_reason

    def test_rain_series_is_discovered_but_not_modeled(self):
        c = ks.parse_market(kalshi_markets()[0], kalshi_series("KXRAINNYC"), NOW)
        assert not c.modeled and "rain" in c.unmodeled_reason

    def test_celsius_and_unsupported_fee_are_not_modeled(self):
        m = copy.deepcopy(kalshi_markets()[0])
        m["rules_primary"] += " in Celsius"
        assert not ks.parse_market(m, kalshi_series(), NOW).modeled
        s = dict(kalshi_series(), fee_type="flat")
        assert not ks.parse_market(kalshi_markets()[0], s, NOW).modeled

    def test_garbage_market_never_raises(self):
        c = ks.parse_market({"ticker": "X", "event_ticker": "NOPE"}, kalshi_series(), NOW)
        assert not c.modeled

    def test_series_classification(self):
        got = {s["ticker"]: ks.classify_series(s) for s in load("kalshi_series_list.json")}
        assert got["KXHIGHNY"] == "high" and got["KXLOWTNYC"] == "low" and got["KXRAINNYC"] == "rain"


class TestKalshiQuotes:
    def test_orderbook_converts_no_bids_into_yes_asks_exactly(self):
        q = ks.quote_from_orderbook(load("kalshi_orderbook.json"), NOW)
        assert q.depth_known and q.yes_asks and q.no_asks
        ob = load("kalshi_orderbook.json")["orderbook_fp"]
        best_no_bid = max(D(p) for p, _ in ob["no_dollars"])
        best_yes_bid = max(D(p) for p, _ in ob["yes_dollars"])
        assert q.yes_ask == 1 - best_no_bid and q.no_ask == 1 - best_yes_bid
        assert q.yes_asks[0].price + best_no_bid == D(1)

    def test_asks_are_sorted_best_first_regardless_of_input_order(self):
        book = {"orderbook_fp": {"yes_dollars": [], "no_dollars": [["0.10", "5"], ["0.60", "5"], ["0.30", "5"]]}}
        q = ks.quote_from_orderbook(book, NOW)
        assert [lv.price for lv in q.yes_asks] == [D("0.40"), D("0.70"), D("0.90")]

    def test_empty_and_malformed_books_yield_no_asks(self):
        assert ks.quote_from_orderbook({}, NOW).yes_asks == ()
        bad = {"orderbook_fp": {"yes_dollars": [["x", "1"], ["0.5", "0"]], "no_dollars": [["1.0", "5"], ["0", "5"]]}}
        q = ks.quote_from_orderbook(bad, NOW)
        assert q.yes_asks == () and q.no_asks == ()

    def test_market_list_quote_is_top_of_book_only(self):
        m = kalshi_markets()[0]
        q = ks.quote_from_market(m, NOW)
        assert not q.depth_known and len(q.yes_asks) <= 1

    def test_settlement_result_requires_a_final_yes_no(self):
        assert ks.settlement_result({"result": "", "expiration_value": ""}) == (None, None)
        assert ks.settlement_result({"result": "no", "expiration_value": "74.00"}) == ("no", 74.0)
        settled = [m for m in kalshi_markets("kalshi_markets_KXHIGHNY_settled.json") if m.get("result") in ("yes", "no")]
        assert settled and all(ks.settlement_result(m)[1] is not None for m in settled)

    def test_settled_results_are_consistent_with_their_bins(self):
        from src.weather_edge.settlement import value_consistent
        for m in kalshi_markets("kalshi_markets_KXHIGHNY_settled.json"):
            c = ks.parse_market(m, kalshi_series(), NOW)
            res, val = ks.settlement_result(m)
            if c.modeled and res:
                assert value_consistent(c.bin.lo, c.bin.hi, val, res), m["ticker"]


class TestPolymarketParser:
    def event(self):
        return load("polymarket_event_nyc_high.json")

    def test_event_parses_to_noaa_laguardia_bins(self):
        cs = pm.parse_event(self.event(), NOW)
        assert len(cs) == 11 and all(c.modeled for c in cs), [c.unmodeled_reason for c in cs if not c.modeled]
        c = cs[0]
        assert c.platform == "polymarket" and c.station_icao == "KLGA" and c.metric == "high"
        assert c.target_date == date(2026, 10, 11) and c.fee_model == "polymarket_curve" and c.fee_param == D("0.05")
        assert c.yes_token and c.no_token and c.yes_token != c.no_token and c.min_order == 5
        assert [x.bin for x in cs][0] == Bin(None, 55) and [x.bin for x in cs][-1] == Bin(74, None)

    def test_bins_form_a_partition(self):
        from src.weather_edge.probability import check_partition
        assert check_partition([c.bin for c in pm.parse_event(self.event(), NOW)])[0]

    def test_kalshi_and_polymarket_nyc_use_different_stations(self):
        k = ks.parse_market(kalshi_markets()[0], kalshi_series(), NOW)
        p = pm.parse_event(self.event(), NOW)[0]
        assert (k.station_icao, p.station_icao) == ("KNYC", "KLGA")

    def test_double_encoded_degree_signs_are_tolerated(self):
        assert pm.bin_from_label("56-57Â°F") == Bin(56, 57)
        assert pm.bin_from_label("55Â°F or below") == Bin(None, 55)
        assert pm.bin_from_label("74°F or higher") == Bin(74, None)

    def test_celsius_labels_are_never_read_as_fahrenheit(self):
        assert pm.bin_from_label("12°C or below") is None
        assert pm.bin_from_label("13-14°C") is None

    def test_other_events_are_classified_and_never_silently_modeled(self):
        for ev in load("polymarket_event_other.json"):
            kind = pm.classify_event(ev)
            cs = pm.parse_event(ev, NOW)
            if kind != "temperature_daily":
                assert not any(c.modeled for c in cs)
        assert pm.classify_event({"title": "Precipitation in Seattle in September?"}) == "precipitation"
        assert pm.classify_event({"title": "Will a hurricane hit Florida?"}) == "storm"

    def test_unknown_station_and_missing_tokens_are_not_modeled(self):
        ev = copy.deepcopy(self.event())
        ev["resolutionSource"] = "https://www.weather.gov/wrh/timeseries?site=kzzz"
        assert not any(c.modeled for c in pm.parse_event(ev, NOW))
        ev = copy.deepcopy(self.event())
        ev["markets"][0]["clobTokenIds"] = "[]"
        assert not pm.parse_event(ev, NOW)[0].modeled

    def test_closed_or_not_accepting_orders_is_not_open(self):
        ev = copy.deepcopy(self.event())
        ev["markets"][0]["acceptingOrders"] = False
        ev["markets"][1]["closed"] = True
        cs = pm.parse_event(ev, NOW)
        assert cs[0].status == "closed" and cs[1].status == "closed" and cs[2].status == "open"

    def test_celsius_bins_get_an_explicit_reason(self):
        ev = copy.deepcopy(self.event())
        ev["markets"][0]["groupItemTitle"] = "12°C or below"
        c = pm.parse_event(ev, NOW)[0]
        assert not c.modeled and c.unmodeled_reason.startswith("Celsius")

    def test_celsius_event_description_is_not_modeled(self):
        ev = copy.deepcopy(self.event())
        ev["description"] = ev["description"].replace("Fahrenheit", "Celsius")
        assert not any(c.modeled for c in pm.parse_event(ev, NOW))


class TestPolymarketBooks:
    def test_book_levels_are_sorted_even_when_the_api_returns_them_unsorted(self):
        b = load("polymarket_book.json")
        shuffled = copy.deepcopy(b)
        shuffled["asks"] = [{"price": "0.9", "size": "5"}, {"price": "0.5", "size": "5"}, {"price": "0.7", "size": "5"}]
        shuffled["bids"] = [{"price": "0.1", "size": "5"}, {"price": "0.3", "size": "5"}]
        q = pm.quote_from_books(shuffled, shuffled, NOW)
        assert [lv.price for lv in q.yes_asks] == [D("0.5"), D("0.7"), D("0.9")]
        assert q.yes_bid == D("0.3") and q.depth_known

    def test_real_book_fixture_parses(self):
        q = pm.quote_from_books(load("polymarket_book.json"), None, NOW)
        assert q.yes_ask == D("0.999") and q.yes_bid == D("0.991") and q.no_asks == ()

    def test_gamma_quote_is_top_of_book_and_derives_no_ask_from_yes_bid(self):
        m = {"bestAsk": 0.30, "bestBid": 0.28, "orderMinSize": 5, "liquidityNum": 1000}
        q = pm.quote_from_gamma(m, NOW)
        assert q.yes_ask == D("0.3") and q.no_ask == D("0.72") and not q.depth_known

    def test_settlement_needs_a_resolved_clean_price_pair(self):
        ok = {"closed": True, "umaResolutionStatus": "resolved", "outcomePrices": '["1", "0"]'}
        assert pm.settlement_result(ok) == "yes"
        assert pm.settlement_result({**ok, "outcomePrices": '["0", "1"]'}) == "no"
        assert pm.settlement_result({**ok, "outcomePrices": '["0.5", "0.5"]'}) is None
        assert pm.settlement_result({**ok, "closed": False}) is None
        assert pm.settlement_result({**ok, "umaResolutionStatus": "proposed"}) is None


class _Resp:
    def __init__(self, status, payload=None, headers=None):
        self.status_code, self._p, self.headers = status, payload, headers or {}

    def json(self):
        if self._p is None:
            raise ValueError("bad json")
        return self._p


class _Session:
    def __init__(self, script):
        self.script, self.headers, self.calls = list(script), {}, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _client(script, **kw):
    sleeps = []
    return PublicHttpClient("t", "https://x.test", session=_Session(script), sleep=sleeps.append,
                            clock=lambda: 0.0, **kw), sleeps


class TestHttpClient:
    def test_success_and_timeout_and_user_agent(self):
        c, _ = _client([_Resp(200, {"ok": 1})])
        assert c.get_json("/p", {"a": 1}) == {"ok": 1}
        assert c.session.calls[0][2] == 20.0 and "User-Agent" in c.session.headers

    def test_retries_429_and_5xx_with_backoff_then_succeeds(self):
        c, sleeps = _client([_Resp(429, headers={"Retry-After": "3"}), _Resp(503), _Resp(200, {"ok": 1})])
        assert c.get_json("/p") == {"ok": 1}
        assert c.calls == 3 and 3.0 in sleeps and any(s >= 2.0 for s in sleeps)

    def test_network_errors_are_retried_then_raise_provider_error(self):
        c, _ = _client([requests.Timeout("x")] * 4, max_retries=3)
        with pytest.raises(ProviderError):
            c.get_json("/p")
        assert c.calls == 4

    def test_client_errors_are_not_retried(self):
        c, _ = _client([_Resp(404)])
        with pytest.raises(ProviderError) as e:
            c.get_json("/p")
        assert e.value.status == 404 and c.calls == 1

    def test_invalid_json_raises(self):
        c, _ = _client([_Resp(200, None)])
        with pytest.raises(ProviderError):
            c.get_json("/p")

    def test_error_message_never_leaks_query_strings(self):
        c, _ = _client([_Resp(500)] * 4, max_retries=3)
        with pytest.raises(ProviderError) as e:
            c.get_json("/p", {"apikey": "SECRET"})
        assert "SECRET" not in str(e.value)

    def test_calls_are_paced(self):
        sleeps = []
        c = PublicHttpClient("t", "https://x", session=_Session([_Resp(200, {}), _Resp(200, {})]), min_interval=1.0,
                             sleep=sleeps.append, clock=lambda: 0.0)
        c.get_json("/a"); c.get_json("/b")
        assert any(s > 0 for s in sleeps)
