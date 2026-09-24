"""Kalshi recommendation -> exact contract mapping.

Fixture tests/fixtures/kalshi_game_markets.json holds UNMODIFIED live
payloads from Kalshi's public GET /markets (captured 2026-09-23, no auth,
no orders). Everything here is offline. Fail-closed behavior is proven by
mutating real payloads / recommendation fields one dimension at a time.
"""

import copy
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.execution.base import Market
from src.execution.kalshi import KalshiProvider
from src.execution.kalshi_mapping import SUPPORTED_SERIES, parse_kalshi_contract
from src.execution.matching import (
    build_recommendation_event, find_best_match, provider_event_from_market,
)
from src.execution.team_codes import TEAM_TABLES, team_code_for_name

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "kalshi_game_markets.json").read_text(encoding="utf-8")
)
ALL_MARKETS = [m for k, v in FIXTURE.items() if not k.startswith("_") for m in v]


@pytest.fixture(scope="module")
def provider():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ).decode()
    return KalshiProvider(api_key_id="test-key", private_key_pem=pem)


def _market(raw):
    return Market(id=raw["ticker"], title=raw["yes_sub_title"], status=raw["status"], raw=raw)


def _candidates(provider, raws=None):
    out = []
    for raw in (raws if raws is not None else ALL_MARKETS):
        m = _market(raw)
        pe = provider_event_from_market("kalshi", m, provider.parse_game_event(m))
        if pe is not None:
            out.append(pe)
    return out


def _by_ticker(ticker):
    return next(m for m in ALL_MARKETS if m["ticker"] == ticker)


def _rec(market_type, matchup, side, start, league, line=None, raw_line=None, rid="rec-1"):
    row = {
        "recommendation_id": rid, "league": league, "market_type": market_type,
        "matchup": matchup, "side": side, "line": line, "raw_line": raw_line,
        "event_start_time": start,
    }
    return build_recommendation_event(row)


NFL_ATL_NO = ("Atlanta Falcons @ New Orleans Saints", "2026-10-06T00:15:00+00:00")   # Mon Oct 5, 8:15 PM ET
NFL_PHI_CHI = ("Philadelphia Eagles @ Chicago Bears", "2026-09-29T00:20:00+00:00")   # Sun Sep 28 ET
MLB_AZ_SD = ("Arizona Diamondbacks @ San Diego Padres", "2026-09-27T00:40:00+00:00")  # Sep 26 8:40 PM EDT


def _match(provider, rec, raws=None):
    return find_best_match(rec, _candidates(provider, raws), 0.98)


def _lift_tie_guard(monkeypatch):
    """The NFL fixtures exercise the mapping LOGIC; the tie guard that makes
    NFL moneyline unsupported by default is tested separately below."""
    import src.execution.matching as matching
    monkeypatch.setattr(matching, "TIE_POSSIBLE_MONEYLINE_LEAGUES", frozenset())


# ── real payload structure ───────────────────────────────────────────


class TestRealPayloadParsing:
    def test_every_real_fixture_market_parses(self):
        for raw in ALL_MARKETS:
            contract, reason = parse_kalshi_contract(raw)
            assert contract is not None, (raw["ticker"], reason)

    def test_moneyline_is_one_market_per_team_yes_means_that_team_wins(self):
        no, atl = FIXTURE["KXNFLGAME"]
        c_no, _ = parse_kalshi_contract(no)
        c_atl, _ = parse_kalshi_contract(atl)
        assert c_no.kind == c_atl.kind == "moneyline"
        assert c_no.yes_team == "New Orleans Saints" and c_atl.yes_team == "Atlanta Falcons"
        assert {c_no.team_a, c_no.team_b} == {"Atlanta Falcons", "New Orleans Saints"}
        assert c_no.line is None

    def test_spread_is_team_wins_by_over_floor_strike(self):
        c, _ = parse_kalshi_contract(_by_ticker("KXNFLSPREAD-26SEP28PHICHI-PHI8"))
        assert (c.kind, c.yes_team, c.line) == ("spread", "Philadelphia Eagles", 7.5)

    def test_total_is_over_floor_strike_with_no_team(self):
        c, _ = parse_kalshi_contract(_by_ticker("KXNFLTOTAL-26SEP28PHICHI-66"))
        assert (c.kind, c.yes_team, c.line) == ("total", None, 65.5)

    def test_date_is_the_eastern_calendar_date_from_the_ticker(self):
        # A Monday-night NFL game starts on the next UTC day but is dated Oct 5.
        c, _ = parse_kalshi_contract(FIXTURE["KXNFLGAME"][0])
        assert c.event_date.isoformat() == "2026-10-05"
        assert c.event_start_time is None   # NFL tickers carry no time

    def test_mlb_ticker_time_is_eastern_and_converted_to_utc(self):
        c, _ = parse_kalshi_contract(FIXTURE["KXMLBGAME"][0])
        assert c.event_start_time == datetime(2026, 9, 27, 0, 40, tzinfo=timezone.utc)

    def test_every_team_code_table_entry_has_full_name_and_label(self):
        for league, table in TEAM_TABLES.items():
            for code, (name, labels, _aliases) in table.items():
                assert name and labels, (league, code)
                assert team_code_for_name(league, name) == code

    def test_ambiguous_city_only_names_are_not_resolvable(self):
        for name in ("Chicago", "Los Angeles", "New York", "Kansas City"):
            assert team_code_for_name("MLB", name) is None


class TestUnsupportedShapesFailClosed:
    def _mutate(self, ticker, **changes):
        raw = copy.deepcopy(_by_ticker(ticker))
        raw.update(changes)
        return parse_kalshi_contract(raw)

    def test_period_and_team_total_series_are_unsupported(self):
        raw = copy.deepcopy(_by_ticker("KXNFLTOTAL-26SEP28PHICHI-66"))
        for series in ("KXNFL1HTOTAL", "KXMLBF5TOTAL", "KXNFLTEAMTOTAL"):
            r = copy.deepcopy(raw)
            r["event_ticker"] = raw["event_ticker"].replace("KXNFLTOTAL", series)
            r["ticker"] = raw["ticker"].replace("KXNFLTOTAL", series)
            assert parse_kalshi_contract(r) == (None, "unsupported_series")

    def test_ncaaf_and_player_prop_series_are_unsupported(self):
        for series in ("KXNCAAFGAME", "KXNFLPASSYDS"):
            assert series not in SUPPORTED_SERIES
        raw = copy.deepcopy(FIXTURE["KXNFLGAME"][0])
        raw["event_ticker"] = "KXNCAAFGAME-26OCT06USMTROY"
        raw["ticker"] = "KXNCAAFGAME-26OCT06USMTROY-USM"
        assert parse_kalshi_contract(raw)[0] is None

    def test_combo_style_custom_strike_is_rejected(self):
        contract, reason = self._mutate(
            "KXNFLGAME-26OCT05ATLNO-NO",
            custom_strike={"Associated Events": "x", "Associated Market Sides": "y"},
        )
        assert contract is None and reason == "unexpected_custom_strike"

    def test_inactive_market_is_rejected(self):
        assert self._mutate("KXNFLGAME-26OCT05ATLNO-NO", status="finalized")[1] == "market_not_active"

    def test_whole_number_or_capped_strike_is_rejected(self):
        assert self._mutate("KXNFLSPREAD-26SEP28PHICHI-PHI8", floor_strike=7.0)[0] is None
        assert self._mutate("KXNFLSPREAD-26SEP28PHICHI-PHI8", cap_strike=9.5)[1] == "unexpected_cap_strike"

    def test_label_that_disagrees_with_ticker_code_is_rejected(self):
        c, reason = self._mutate("KXNFLGAME-26OCT05ATLNO-NO", yes_sub_title="Atlanta")
        assert c is None and reason == "label_code_mismatch"

    def test_ticker_strike_disagreement_is_rejected(self):
        c, reason = self._mutate("KXNFLTOTAL-26SEP28PHICHI-66", floor_strike=62.5)
        assert c is None and reason == "ticker_strike_mismatch"

    def test_unknown_team_code_all_star_side_is_rejected(self):
        raw = copy.deepcopy(FIXTURE["KXWNBAGAME"][0])
        raw["event_ticker"] = "KXWNBAGAME-26SEP24COOSPN"
        raw["ticker"] = "KXWNBAGAME-26SEP24COOSPN-COO"
        raw["yes_sub_title"] = "Team Coop"
        assert parse_kalshi_contract(raw) == (None, "unknown_or_ambiguous_team_codes")

    def test_garbage_input_never_raises(self):
        for junk in (None, {}, {"ticker": 5}, {"event_ticker": "KXNFLGAME"}, "x"):
            assert parse_kalshi_contract(junk)[0] is None


# ── recommendation -> exact contract ─────────────────────────────────


class TestMoneylineExactMapping:
    def test_away_pick_maps_to_the_away_teams_yes_contract(self, provider, monkeypatch):
        _lift_tie_guard(monkeypatch)
        rec = _rec("game_moneyline", NFL_ATL_NO[0], "AWAY", NFL_ATL_NO[1], "NFL")
        m = _match(provider, rec)
        assert (m.provider_market_id, m.provider_side) == ("KXNFLGAME-26OCT05ATLNO-ATL", "YES")

    def test_home_pick_maps_to_the_home_teams_yes_contract(self, provider, monkeypatch):
        _lift_tie_guard(monkeypatch)
        rec = _rec("game_moneyline", NFL_ATL_NO[0], "HOME", NFL_ATL_NO[1], "NFL")
        m = _match(provider, rec)
        assert (m.provider_market_id, m.provider_side) == ("KXNFLGAME-26OCT05ATLNO-NO", "YES")

    def test_every_league_maps_both_sides(self, provider):
        cases = [
            ("MLB", MLB_AZ_SD, "KXMLBGAME-26SEP262040AZSD-AZ", "KXMLBGAME-26SEP262040AZSD-SD"),
            ("WNBA", ("Las Vegas Aces @ Phoenix Mercury", "2026-09-25T00:00:00+00:00"),
             "KXWNBAGAME-26SEP24LVPHX-LV", "KXWNBAGAME-26SEP24LVPHX-PHX"),
        ]
        for league, (matchup, start), away_ticker, home_ticker in cases:
            away = _match(provider, _rec("game_moneyline", matchup, "AWAY", start, league))
            home = _match(provider, _rec("game_moneyline", matchup, "HOME", start, league))
            assert away.provider_market_id == away_ticker
            assert home.provider_market_id == home_ticker

    def test_wrong_team_fails_closed(self, provider):
        rec = _rec("game_moneyline", "Dallas Cowboys @ Houston Texans", "AWAY", NFL_ATL_NO[1], "NFL")
        assert _match(provider, rec) is None

    def test_one_correct_team_one_wrong_team_fails_closed(self, provider):
        rec = _rec("game_moneyline", "Atlanta Falcons @ Houston Texans", "AWAY", NFL_ATL_NO[1], "NFL")
        assert _match(provider, rec) is None

    def test_wrong_event_date_fails_closed(self, provider):
        rec = _rec("game_moneyline", NFL_ATL_NO[0], "AWAY", "2026-10-13T00:15:00+00:00", "NFL")
        assert _match(provider, rec) is None

    def test_wrong_league_fails_closed(self, provider):
        rec = _rec("game_moneyline", NFL_ATL_NO[0], "AWAY", NFL_ATL_NO[1], "MLB")
        assert _match(provider, rec) is None

    def test_missing_start_time_fails_closed(self, provider):
        rec = _rec("game_moneyline", NFL_ATL_NO[0], "AWAY", None, "NFL")
        assert _match(provider, rec) is None

    def test_unrecognized_side_fails_closed(self, provider):
        rec = _rec("game_moneyline", NFL_ATL_NO[0], "DRAW", NFL_ATL_NO[1], "NFL")
        assert _match(provider, rec) is None

    def test_unknown_team_name_fails_closed(self, provider):
        rec = _rec("game_moneyline", "Atlantis Falcons @ New Orleans Saints", "AWAY", NFL_ATL_NO[1], "NFL")
        assert _match(provider, rec) is None

    def test_mlb_wrong_start_time_same_date_fails_closed(self, provider):
        # 23:40 ET the same evening -- same Eastern date but not this game
        rec = _rec("game_moneyline", MLB_AZ_SD[0], "AWAY", "2026-09-27T03:40:00+00:00", "MLB")
        assert _match(provider, rec) is None

    def test_mlb_doubleheader_with_two_matching_events_is_ambiguous(self, provider):
        raws = []
        for hhmm in ("2040", "2050"):
            for raw in FIXTURE["KXMLBGAME"]:
                r = copy.deepcopy(raw)
                r["event_ticker"] = raw["event_ticker"].replace("2040", hhmm)
                r["ticker"] = raw["ticker"].replace("2040", hhmm)
                raws.append(r)
        rec = _rec("game_moneyline", MLB_AZ_SD[0], "AWAY", MLB_AZ_SD[1], "MLB")
        assert _match(provider, rec, raws) is None

    def test_opponents_market_is_never_used_via_no(self, provider):
        only_home = [m for m in FIXTURE["KXNFLGAME"] if m["ticker"].endswith("-NO")]
        rec = _rec("game_moneyline", NFL_ATL_NO[0], "AWAY", NFL_ATL_NO[1], "NFL")
        assert _match(provider, rec, only_home) is None


class TestSpreadExactMapping:
    def test_laying_points_maps_to_yes_on_that_teams_wins_by_over(self, provider):
        rec = _rec("game_spread_ou", NFL_PHI_CHI[0], "AWAY", NFL_PHI_CHI[1], "NFL",
                   line=7.5, raw_line=-7.5)
        m = _match(provider, rec)
        assert (m.provider_market_id, m.provider_side) == ("KXNFLSPREAD-26SEP28PHICHI-PHI8", "YES")

    def test_getting_points_maps_to_no_on_the_opponents_wins_by_over(self, provider):
        # Chicago +6.5 covers iff Philadelphia does NOT win by more than 6.5
        rec = _rec("game_spread_ou", NFL_PHI_CHI[0], "HOME", NFL_PHI_CHI[1], "NFL",
                   line=6.5, raw_line=6.5)
        m = _match(provider, rec)
        assert (m.provider_market_id, m.provider_side) == ("KXNFLSPREAD-26SEP28PHICHI-PHI7", "NO")

    def test_unsigned_line_is_not_trusted(self, provider):
        rec = _rec("game_spread_ou", NFL_PHI_CHI[0], "AWAY", NFL_PHI_CHI[1], "NFL",
                   line=7.5, raw_line=None)
        assert _match(provider, rec) is None

    def test_wrong_line_fails_closed(self, provider):
        for raw_line in (-8.5, -12.5, -6.0, -7.0):
            rec = _rec("game_spread_ou", NFL_PHI_CHI[0], "AWAY", NFL_PHI_CHI[1], "NFL",
                       line=abs(raw_line), raw_line=raw_line)
            assert _match(provider, rec) is None, raw_line

    def test_wrong_sign_for_the_market_available_fails_closed(self, provider):
        # PHI getting +7.5 needs CHI's market, which the fixture doesn't contain
        rec = _rec("game_spread_ou", NFL_PHI_CHI[0], "AWAY", NFL_PHI_CHI[1], "NFL",
                   line=7.5, raw_line=7.5)
        assert _match(provider, rec) is None

    def test_pickem_fails_closed(self, provider):
        rec = _rec("game_spread_ou", NFL_PHI_CHI[0], "AWAY", NFL_PHI_CHI[1], "NFL",
                   line=0.0, raw_line=0.0)
        assert _match(provider, rec) is None

    def test_wrong_team_fails_closed(self, provider):
        rec = _rec("game_spread_ou", "Dallas Cowboys @ Houston Texans", "AWAY", NFL_PHI_CHI[1], "NFL",
                   line=7.5, raw_line=-7.5)
        assert _match(provider, rec) is None

    def test_mlb_run_line_and_wnba_spread(self, provider):
        mlb = _rec("game_runline_ou", "Arizona Diamondbacks @ Colorado Rockies",
                   "AWAY", "2026-09-24T00:40:00+00:00", "MLB", line=6.5, raw_line=-6.5)
        m = _match(provider, mlb)
        assert (m.provider_market_id, m.provider_side) == ("KXMLBSPREAD-26SEP232040AZCOL-AZ7", "YES")
        # WNBA: New York laying 6.5 -> YES on "NY wins by over 6.5" (NY7)
        wnba_lay = _rec("game_spread_ou", "Atlanta Dream @ New York Liberty",
                        "HOME", "2026-09-24T00:00:00+00:00", "WNBA", line=6.5, raw_line=-6.5)
        m = _match(provider, wnba_lay)
        assert (m.provider_market_id, m.provider_side) == ("KXWNBASPREAD-26SEP23ATLNY-NY7", "YES")
        # New York getting 14.5 -> NO on "ATL wins by over 14.5" (ATL15)
        wnba_get = _rec("game_spread_ou", "Atlanta Dream @ New York Liberty",
                        "HOME", "2026-09-24T00:00:00+00:00", "WNBA", line=14.5, raw_line=14.5, rid="r2")
        m = _match(provider, wnba_get)
        assert (m.provider_market_id, m.provider_side) == ("KXWNBASPREAD-26SEP23ATLNY-ATL15", "NO")
        # a line the fixture has no market for must fail closed, never approximate
        assert _match(provider, _rec("game_spread_ou", "Atlanta Dream @ New York Liberty",
                                     "HOME", "2026-09-24T00:00:00+00:00", "WNBA",
                                     line=9.5, raw_line=-9.5, rid="r3")) is None


class TestTotalExactMapping:
    def test_over_maps_to_yes_and_under_to_no_on_the_same_market(self, provider):
        over = _rec("game_total_ou", NFL_PHI_CHI[0], "OVER", NFL_PHI_CHI[1], "NFL", line=65.5)
        under = _rec("game_total_ou", NFL_PHI_CHI[0], "UNDER", NFL_PHI_CHI[1], "NFL", line=65.5, rid="r2")
        mo, mu = _match(provider, over), _match(provider, under)
        assert (mo.provider_market_id, mo.provider_side) == ("KXNFLTOTAL-26SEP28PHICHI-66", "YES")
        assert (mu.provider_market_id, mu.provider_side) == ("KXNFLTOTAL-26SEP28PHICHI-66", "NO")

    def test_wrong_line_fails_closed(self, provider):
        for line in (66.0, 64.5, 65.0, 66.5):
            rec = _rec("game_total_ou", NFL_PHI_CHI[0], "OVER", NFL_PHI_CHI[1], "NFL", line=line)
            assert _match(provider, rec) is None, line

    def test_wrong_event_fails_closed(self, provider):
        rec = _rec("game_total_ou", "Dallas Cowboys @ Houston Texans", "OVER", NFL_PHI_CHI[1], "NFL", line=65.5)
        assert _match(provider, rec) is None

    def test_side_that_is_not_over_under_fails_closed(self, provider):
        rec = _rec("game_total_ou", NFL_PHI_CHI[0], "HOME", NFL_PHI_CHI[1], "NFL", line=65.5)
        assert _match(provider, rec) is None

    def test_mlb_and_wnba_totals(self, provider):
        mlb = _rec("game_total_ou", "Chicago White Sox @ Kansas City Royals",
                   "OVER", "2026-09-23T23:40:00+00:00", "MLB", line=15.5)
        assert _match(provider, mlb).provider_market_id == "KXMLBTOTAL-26SEP231940CWSKC-16"
        wnba = _rec("game_total_ou", "Dallas Wings @ Seattle Storm",
                    "UNDER", "2026-09-24T02:00:00+00:00", "WNBA", line=181.5)
        m = _match(provider, wnba)
        assert (m.provider_market_id, m.provider_side) == ("KXWNBATOTAL-26SEP23DALSEA-182", "NO")


class TestCrossTypeSafety:
    def test_a_moneyline_rec_never_maps_to_spread_or_total_markets(self, provider):
        rec = _rec("game_moneyline", NFL_PHI_CHI[0], "AWAY", NFL_PHI_CHI[1], "NFL")
        assert _match(provider, rec) is None   # fixture has only spread/total for PHI-CHI

    def test_unsupported_market_types_have_no_candidates(self, provider):
        row = {"recommendation_id": "p", "league": "NFL", "market_type": "player_pass_yds",
               "matchup": NFL_PHI_CHI[0], "side": "OVER", "line": 250.5,
               "event_start_time": NFL_PHI_CHI[1]}
        assert build_recommendation_event(row) is None


class TestGetGameMarkets:
    def test_fetches_only_verified_series_paginates_and_dedupes(self, provider):
        calls = []

        def fake_get(endpoint, params=None, **_kw):
            calls.append((endpoint, dict(params)))
            series = params["series_ticker"]
            if series == "KXNFLGAME" and "cursor" not in params:
                return {"markets": [FIXTURE["KXNFLGAME"][0]], "cursor": "c2"}
            if series == "KXNFLGAME":
                return {"markets": [FIXTURE["KXNFLGAME"][0], FIXTURE["KXNFLGAME"][1]], "cursor": ""}
            return {"markets": [], "cursor": ""}

        from unittest import mock
        with mock.patch.object(provider, "_get", side_effect=fake_get):
            markets = provider.get_game_markets(["NFL"])
        assert {c[1]["series_ticker"] for c in calls} == {"KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL"}
        assert all(c[0] == "/markets" and c[1]["status"] == "open" for c in calls)
        assert [m.id for m in markets] == [
            "KXNFLGAME-26OCT05ATLNO-NO", "KXNFLGAME-26OCT05ATLNO-ATL",
        ]

    def test_one_failing_series_does_not_hide_the_others(self, provider):
        from unittest import mock

        def fake_get(endpoint, params=None, **_kw):
            if params["series_ticker"] == "KXNFLSPREAD":
                raise RuntimeError("boom")
            return {"markets": [FIXTURE["KXNFLGAME"][0]] if params["series_ticker"] == "KXNFLGAME" else [], "cursor": ""}

        with mock.patch.object(provider, "_get", side_effect=fake_get):
            assert len(provider.get_game_markets(["NFL"])) == 1

    def test_total_outage_raises_instead_of_returning_a_silent_empty_list(self, provider):
        from unittest import mock
        with mock.patch.object(provider, "_get", side_effect=RuntimeError("down")):
            with pytest.raises(RuntimeError):
                provider.get_game_markets(["NFL"])

    def test_never_calls_a_write_path(self, provider):
        from unittest import mock
        with mock.patch.object(provider, "_get", return_value={"markets": [], "cursor": ""}), \
             mock.patch.object(provider.session, "post") as post, \
             mock.patch.object(provider.session, "delete") as delete:
            provider.get_game_markets()
        post.assert_not_called()
        delete.assert_not_called()


class TestTieCapableMoneylineFailsClosed:
    """Kalshi's settlement of an NFL tie is unverified and must not be
    inferred from sportsbook grading (where a tie is a push)."""

    def test_nfl_moneyline_is_unsupported_by_default_for_both_sides(self, provider):
        for side in ("AWAY", "HOME"):
            rec = _rec("game_moneyline", NFL_ATL_NO[0], side, NFL_ATL_NO[1], "NFL")
            assert _match(provider, rec) is None

    def test_reason_is_explicit(self, provider):
        from src.execution.matching import resolve_strict_side
        rec = _rec("game_moneyline", NFL_ATL_NO[0], "AWAY", NFL_ATL_NO[1], "NFL")
        ev = next(c for c in _candidates(provider) if c.market_id.endswith("ATLNO-ATL"))
        assert resolve_strict_side(rec, ev) == (None, "tie_settlement_unverified")

    def test_nfl_spread_and_total_are_unaffected_because_half_point_lines_cannot_push(self, provider):
        spread = _rec("game_spread_ou", NFL_PHI_CHI[0], "AWAY", NFL_PHI_CHI[1], "NFL", line=7.5, raw_line=-7.5)
        total = _rec("game_total_ou", NFL_PHI_CHI[0], "OVER", NFL_PHI_CHI[1], "NFL", line=65.5)
        assert _match(provider, spread) is not None
        assert _match(provider, total) is not None

    def test_mlb_and_wnba_moneyline_cannot_tie_and_stay_supported(self, provider):
        assert _match(provider, _rec("game_moneyline", MLB_AZ_SD[0], "AWAY", MLB_AZ_SD[1], "MLB")) is not None
        wnba = _rec("game_moneyline", "Las Vegas Aces @ Phoenix Mercury", "HOME", "2026-09-25T00:00:00+00:00", "WNBA")
        assert _match(provider, wnba) is not None
