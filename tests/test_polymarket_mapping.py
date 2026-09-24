"""Polymarket US recommendation -> exact moneyline contract mapping.

Fixture tests/fixtures/polymarket_us_moneyline_markets.json holds real
payloads (structure-relevant keys, values unmodified) from Polymarket US's
public GET /v1/markets?sportsMarketType=moneyline, captured 2026-09-23.
The captured games are historical (already closed), so tests that need an
OPEN market set closed=False explicitly on a copy -- that one field is the
only mutation, called out in each helper.
"""

import copy
import json
from pathlib import Path
from unittest import mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
import base64

from src.execution.base import Market
from src.execution.matching import build_recommendation_event, find_best_match, provider_event_from_market
from src.execution.polymarket_mapping import parse_polymarket_moneyline
from src.execution.polymarket_us import PolymarketUSProvider

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "polymarket_us_moneyline_markets.json").read_text(encoding="utf-8")
)


@pytest.fixture(scope="module")
def provider():
    key = ed25519.Ed25519PrivateKey.generate()
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    return PolymarketUSProvider(api_key_id="test-key", private_key_b64=base64.b64encode(raw).decode())


def _open(raw):
    raw = copy.deepcopy(raw)
    raw["closed"] = False
    return raw


LAC_TEN = _open(FIXTURE["away_is_yes"][0])       # LAC (YES/long, away) at TEN
CAR_GB = _open(FIXTURE["away_is_yes"][1])
START = "2025-11-02T18:00:00+00:00"                # 1:00 PM ET, Nov 2
MATCHUP = "Los Angeles Chargers @ Tennessee Titans"


def _rec(side, matchup=MATCHUP, start=START, league="NFL", market_type="game_moneyline", **extra):
    row = {"recommendation_id": "rec-1", "league": league, "market_type": market_type,
           "matchup": matchup, "side": side, "line": extra.get("line"), "raw_line": extra.get("raw_line"),
           "event_start_time": start}
    return build_recommendation_event(row)


def _match(provider, rec, raws):
    events = []
    for raw in raws:
        m = Market(id=raw["slug"], title=raw["question"], status="active", raw=raw)
        pe = provider_event_from_market("polymarket_us", m, provider.parse_game_event(m))
        if pe is not None:
            events.append(pe)
    return find_best_match(rec, events, 0.98)


class TestParsing:
    def test_real_payloads_parse_when_open(self):
        for raw in (LAC_TEN, CAR_GB):
            contract, reason = parse_polymarket_moneyline(raw)
            assert contract is not None, reason
        c, _ = parse_polymarket_moneyline(LAC_TEN)
        assert (c.league, c.yes_team, c.no_team) == ("NFL", "Los Angeles Chargers", "Tennessee Titans")
        assert c.event_date.isoformat() == "2025-11-02"

    def test_a_closed_market_is_rejected(self):
        raw = copy.deepcopy(FIXTURE["away_is_yes"][0])           # real payload as captured: closed=True
        assert parse_polymarket_moneyline(raw) == (None, "market_not_open")

    def test_unsupported_leagues_fail_closed(self):
        ufc = _open(FIXTURE["home_is_yes"][0])
        ufc["active"] = True
        assert parse_polymarket_moneyline(ufc) == (None, "unsupported_league")
        nba = copy.deepcopy(LAC_TEN)
        for side in nba["marketSides"]:
            side["team"]["league"] = "nba"
        nba["slug"] = nba["slug"].replace("-nfl-", "-nba-")
        for side in nba["marketSides"]:
            side["identifier"] = nba["slug"]
        assert parse_polymarket_moneyline(nba) == (None, "unsupported_league")

    @pytest.mark.parametrize("mutate,reason", [
        (lambda r: r.update(sportsMarketType="futures", marketType="futures"), "not_moneyline"),
        (lambda r: r.update(active=False), "market_not_open"),
        (lambda r: r.update(marketSides=r["marketSides"][:1]), "unexpected_market_sides"),
        (lambda r: r["marketSides"][1].update(long=True), "unexpected_market_sides"),
        (lambda r: r["marketSides"][0].update(identifier="other-slug"), "side_identifier_mismatch"),
        (lambda r: r["marketSides"][0]["team"].update(name="Atlantis Falcons"), "unknown_team"),
        (lambda r: r.update(gameStartTime=None), "unknown_start_time"),
    ])
    def test_deviations_from_the_observed_structure_fail_closed(self, mutate, reason):
        raw = copy.deepcopy(LAC_TEN)
        mutate(raw)
        contract, got = parse_polymarket_moneyline(raw)
        assert contract is None
        assert got == reason

    def test_slug_league_token_must_agree_with_the_teams_league(self):
        raw = copy.deepcopy(LAC_TEN)
        raw["slug"] = raw["slug"].replace("-nfl-", "-mlb-")
        for side in raw["marketSides"]:
            side["identifier"] = raw["slug"]
        assert parse_polymarket_moneyline(raw) == (None, "slug_league_mismatch")

    def test_garbage_never_raises(self):
        for junk in (None, {}, [], "x", {"slug": 1}):
            assert parse_polymarket_moneyline(junk)[0] is None


class TestExactMapping:
    def test_away_pick_is_yes_and_home_pick_is_no_on_the_single_market(self, provider):
        away = _match(provider, _rec("AWAY"), [LAC_TEN, CAR_GB])
        home = _match(provider, _rec("HOME"), [LAC_TEN, CAR_GB])
        assert (away.provider_market_id, away.provider_side) == ("aec-nfl-lac-ten-2025-11-02", "YES")
        assert (home.provider_market_id, home.provider_side) == ("aec-nfl-lac-ten-2025-11-02", "NO")

    def test_mapping_follows_the_payloads_long_side_not_an_away_assumption(self, provider):
        flipped = copy.deepcopy(LAC_TEN)                   # synthetic mutation of a real payload
        flipped["marketSides"][0]["long"], flipped["marketSides"][1]["long"] = False, True
        away = _match(provider, _rec("AWAY"), [flipped])
        home = _match(provider, _rec("HOME"), [flipped])
        assert away.provider_side == "NO"                  # Chargers are now the SHORT side
        assert home.provider_side == "YES"                 # Titans are the LONG side

    def test_wrong_team_fails_closed(self, provider):
        rec = _rec("AWAY", matchup="Dallas Cowboys @ Houston Texans")
        assert _match(provider, rec, [LAC_TEN, CAR_GB]) is None

    def test_wrong_date_or_start_time_fails_closed(self, provider):
        assert _match(provider, _rec("AWAY", start="2025-11-09T18:00:00+00:00"), [LAC_TEN]) is None
        assert _match(provider, _rec("AWAY", start="2025-11-02T23:30:00+00:00"), [LAC_TEN]) is None

    def test_wrong_league_and_unknown_team_fail_closed(self, provider):
        assert _match(provider, _rec("AWAY", league="MLB"), [LAC_TEN]) is None
        assert _match(provider, _rec("AWAY", matchup="Atlantis Chargers @ Tennessee Titans"), [LAC_TEN]) is None

    def test_spread_and_total_recommendations_never_map_to_a_moneyline_market(self, provider):
        spread = _rec("AWAY", market_type="game_spread_ou", line=3.5, raw_line=-3.5)
        total = _rec("OVER", market_type="game_total_ou", line=44.5)
        assert _match(provider, spread, [LAC_TEN]) is None
        assert _match(provider, total, [LAC_TEN]) is None

    def test_two_events_for_the_same_pair_is_ambiguous(self, provider):
        twin = copy.deepcopy(LAC_TEN)
        twin["slug"] = twin["slug"] + "-b"
        for side in twin["marketSides"]:
            side["identifier"] = twin["slug"]
        assert _match(provider, _rec("AWAY"), [LAC_TEN, twin]) is None


class TestGetGameMarkets:
    def test_requests_only_moneyline_markets_and_paginates(self, provider):
        def market(i):
            return Market(id=f"m{i}", title="t", status="active", raw={})

        pages = [[market(i) for i in range(100)], [market(i) for i in range(100, 130)]]
        with mock.patch.object(provider, "get_markets", side_effect=pages) as gm:
            markets = provider.get_game_markets()
        assert len(markets) == 130
        assert all(call.kwargs["sportsMarketType"] == "moneyline" for call in gm.call_args_list)
        assert [call.kwargs["offset"] for call in gm.call_args_list] == [0, 100]

    def test_never_calls_a_write_path(self, provider):
        with mock.patch.object(provider, "get_markets", return_value=[]), \
             mock.patch.object(provider.session, "post") as post:
            provider.get_game_markets()
        post.assert_not_called()
