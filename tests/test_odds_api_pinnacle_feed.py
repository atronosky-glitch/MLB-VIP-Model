"""Tests for src/odds_api_pinnacle_feed.py — The Odds API's own
`pinnacle` bookmaker as the primary Pinnacle source, added 2026-08-26
after an operator audit found the paid Odds-API plan (bought
specifically for Pinnacle access) was never reaching it because every
existing Odds-API call in this codebase hardcodes regions="us", and
Pinnacle is classified under "eu"."""

from __future__ import annotations

from unittest import mock

import pytest

from src.odds_api_pinnacle_feed import (
    OddsAPIPinnacleClient,
    _parse_game_odds_response,
    _parse_props_response,
    _prop_market_type_map_for_league,
    _sport_key_for_league,
    derive_props_targets,
    STATUS_NO_API_KEY,
    STATUS_NETWORK_ERROR,
    STATUS_LEAGUE_NOT_CONFIGURED,
    STATUS_NO_PINNACLE_POSTED,
)
from src.odds_api_client import OddsAPIKeyError


def _game_odds_event(home="Detroit Tigers", away="Tampa Bay Rays"):
    return {
        "id": "evt1", "home_team": home, "away_team": away,
        "bookmakers": [
            {
                "key": "pinnacle", "title": "Pinnacle",
                "last_update": "2026-08-26T15:37:26Z",
                "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": home, "price": -118}, {"name": away, "price": 109},
                    ]},
                    {"key": "spreads", "outcomes": [
                        {"name": home, "price": 176, "point": -1.5},
                        {"name": away, "price": -197, "point": 1.5},
                    ]},
                    {"key": "totals", "outcomes": [
                        {"name": "Over", "price": -107, "point": 7.5},
                        {"name": "Under", "price": -105, "point": 7.5},
                    ]},
                ],
            },
            {"key": "fanduel", "title": "FanDuel", "markets": []},
        ],
    }


class TestParseGameOddsResponse:
    def test_extracts_moneyline_spread_total(self):
        games = _parse_game_odds_response([_game_odds_event()], "MLB")
        by_type = {g.market_type: g for g in games}
        assert set(by_type) == {"game_moneyline", "game_runline_ou", "game_total_ou"}

    def test_moneyline_has_no_line_and_correct_decimals(self):
        games = _parse_game_odds_response([_game_odds_event()], "MLB")
        ml = next(g for g in games if g.market_type == "game_moneyline")
        assert ml.line is None
        assert ml.home_decimal == pytest.approx(1.8474576271186440)  # -118
        assert ml.away_decimal == pytest.approx(2.09)                # +109

    def test_spread_line_is_signed_home_perspective_not_abs_valued(self):
        """Real bug precedent (2026-08-23, direct pinnapi feed):
        collapsing a signed spread to abs(line) silently merged two
        different real bets. The Odds API already returns each side's
        own signed point from that team's perspective — home team's own
        point here (-1.5, home favorite) must be used directly, no flip."""
        games = _parse_game_odds_response([_game_odds_event()], "MLB")
        spread = next(g for g in games if g.market_type == "game_runline_ou")
        assert spread.line == -1.5
        assert spread.home_decimal == pytest.approx(2.76)                # +176
        assert spread.away_decimal == pytest.approx(1.5076142131979695)  # -197

    def test_total_uses_over_under_decimals_not_home_away(self):
        games = _parse_game_odds_response([_game_odds_event()], "MLB")
        total = next(g for g in games if g.market_type == "game_total_ou")
        assert total.line == 7.5
        assert total.home_decimal is None and total.away_decimal is None
        assert total.over_decimal is not None and total.under_decimal is not None

    def test_source_is_odds_api_pinnacle(self):
        games = _parse_game_odds_response([_game_odds_event()], "MLB")
        assert all(g.source == "odds_api_pinnacle" for g in games)

    def test_event_without_pinnacle_bookmaker_produces_nothing(self):
        ev = _game_odds_event()
        ev["bookmakers"] = [b for b in ev["bookmakers"] if b["key"] != "pinnacle"]
        assert _parse_game_odds_response([ev], "MLB") == []

    def test_league_with_no_game_market_config_returns_empty(self):
        assert _parse_game_odds_response([_game_odds_event()], "NOT_A_REAL_LEAGUE") == []

    def test_uses_baseball_run_line_naming_for_mlb_generic_spread_for_nfl(self):
        mlb_games = _parse_game_odds_response([_game_odds_event()], "MLB")
        nfl_games = _parse_game_odds_response([_game_odds_event()], "NFL")
        assert "game_runline_ou" in {g.market_type for g in mlb_games}
        assert "game_spread_ou" in {g.market_type for g in nfl_games}


def _props_event(home="Detroit Tigers", away="Tampa Bay Rays"):
    return {
        "id": "evt1", "home_team": home, "away_team": away,
        "bookmakers": [
            {
                "key": "pinnacle", "title": "Pinnacle",
                "markets": [
                    {
                        "key": "batter_home_runs", "last_update": "2026-08-26T15:42:12Z",
                        "outcomes": [
                            {"name": "Over", "description": "Cedric Mullins", "price": 640, "point": 0.5},
                            {"name": "Under", "description": "Cedric Mullins", "price": -1430, "point": 0.5},
                        ],
                    },
                ],
            },
        ],
    }


class TestParsePropsResponse:
    MLB_MAP = {"batter_home_runs": "batting_homeRuns_ou"}

    def test_extracts_both_sides(self):
        props = _parse_props_response(_props_event(), self.MLB_MAP, "MLB")
        assert len(props) == 1
        p = props[0]
        assert p.player_name == "Cedric Mullins"
        assert p.unit == "HomeRuns"
        assert p.line == 0.5
        assert p.over_american == 640
        assert p.under_american == -1430
        assert p.source == "odds_api_pinnacle"

    def test_market_with_no_unit_mapping_is_skipped(self):
        """WNBA's combo props (points_assists etc.) have no entry in
        PINNACLE_PROP_UNITS_BY_LEAGUE — must be skipped, not crash or
        silently invent a unit."""
        ev = _props_event()
        ev["bookmakers"][0]["markets"][0]["key"] = "player_points_rebounds_assists"
        props = _parse_props_response(
            ev, {"player_points_rebounds_assists": "player_points_rebounds_assists_ou"}, "WNBA",
        )
        assert props == []

    def test_one_sided_outcome_is_not_returned_as_a_prop(self):
        ev = _props_event()
        ev["bookmakers"][0]["markets"][0]["outcomes"] = [
            {"name": "Over", "description": "Cedric Mullins", "price": 640, "point": 0.5},
        ]
        assert _parse_props_response(ev, self.MLB_MAP, "MLB") == []

    def test_mismatched_over_under_lines_are_not_paired(self):
        ev = _props_event()
        ev["bookmakers"][0]["markets"][0]["outcomes"][1]["point"] = 1.5
        assert _parse_props_response(ev, self.MLB_MAP, "MLB") == []

    def test_last_updated_falls_back_to_market_level_when_no_bookmaker_level_field(self):
        """Confirmed live 2026-08-26: unlike the game-odds endpoint, the
        per-event props response has no bookmaker-level last_update,
        only a per-market one."""
        props = _parse_props_response(_props_event(), self.MLB_MAP, "MLB")
        assert props[0].last_updated is not None


class TestSportKeyAndPropMarketMapResolution:
    def test_sport_keys_resolve_for_all_three_leagues(self):
        assert _sport_key_for_league("MLB") == "baseball_mlb"
        assert _sport_key_for_league("NFL") == "americanfootball_nfl"
        assert _sport_key_for_league("WNBA") == "basketball_wnba"

    def test_unknown_league_returns_none(self):
        assert _sport_key_for_league("NOT_A_REAL_LEAGUE") is None

    def test_prop_market_maps_reuse_each_league_s_existing_parser_mapping(self):
        from src.mlb_props_parser import _PROP_MARKET_TYPE as mlb_map
        assert _prop_market_type_map_for_league("MLB") == mlb_map


class TestOddsAPIPinnacleClientStatusHandling:
    def test_no_api_key_status_for_game_odds(self, monkeypatch):
        client = OddsAPIPinnacleClient()
        monkeypatch.setattr(
            client._client, "get_odds",
            mock.Mock(side_effect=OddsAPIKeyError("no key")),
        )
        result = client.get_game_odds("MLB")
        assert result is None
        assert client.last_fetch_status["MLB"] == STATUS_NO_API_KEY

    def test_network_error_status_for_game_odds(self, monkeypatch):
        import requests
        client = OddsAPIPinnacleClient()
        monkeypatch.setattr(
            client._client, "get_odds",
            mock.Mock(side_effect=requests.exceptions.ConnectionError("boom")),
        )
        result = client.get_game_odds("MLB")
        assert result is None
        assert client.last_fetch_status["MLB"] == STATUS_NETWORK_ERROR

    def test_league_not_configured_status(self):
        client = OddsAPIPinnacleClient()
        result = client.get_game_odds("NOT_A_REAL_LEAGUE")
        assert result is None
        assert client.last_fetch_status["NOT_A_REAL_LEAGUE"] == STATUS_LEAGUE_NOT_CONFIGURED

    def test_empty_response_is_no_pinnacle_posted_not_an_error(self, monkeypatch):
        client = OddsAPIPinnacleClient()
        monkeypatch.setattr(
            client._client, "get_odds",
            mock.Mock(return_value=([_game_odds_event()], False)),
        )
        # Strip pinnacle out so parsing legitimately finds nothing
        monkeypatch.setattr(
            "src.odds_api_pinnacle_feed._parse_game_odds_response",
            lambda data, league: [],
        )
        result = client.get_game_odds("MLB")
        assert result is None
        assert client.last_fetch_status["MLB"] == STATUS_NO_PINNACLE_POSTED

    def test_a_direct_pinnapi_style_failure_does_not_affect_this_client(self, monkeypatch):
        """This client has no dependency on PINNAPI_API_KEY at all — a
        separate, independent code path from src/pinnacle_feed.py."""
        client = OddsAPIPinnacleClient()
        monkeypatch.setattr(
            client._client, "get_odds",
            mock.Mock(return_value=([_game_odds_event()], False)),
        )
        result = client.get_game_odds("MLB")
        assert result is not None
        assert client.last_fetch_status["MLB"] == "ok"


class TestOddsAPIPinnacleGameOddsCreditBudget:
    """Game-odds Pinnacle is cheap (3 credits/call) and unthrottled by
    cadence, but must still respect the shared monthly budget's reserve
    — the same real safety net the props fetch already has — so it can
    never itself be the thing that pushes total spend over budget."""

    def test_game_odds_skipped_when_budget_check_fails(self, monkeypatch):
        client = OddsAPIPinnacleClient()
        get_odds_mock = mock.Mock(return_value=([_game_odds_event()], False))
        monkeypatch.setattr(client._client, "get_odds", get_odds_mock)
        monkeypatch.setattr(
            "src.odds_api_credits.credit_budget_check",
            mock.Mock(return_value=(False, "out of budget")),
        )
        result = client.get_game_odds("MLB", conn=mock.MagicMock())
        assert result is None
        get_odds_mock.assert_not_called()

    def test_game_odds_fetched_when_budget_check_passes(self, monkeypatch):
        client = OddsAPIPinnacleClient()
        get_odds_mock = mock.Mock(return_value=([_game_odds_event()], False))
        monkeypatch.setattr(client._client, "get_odds", get_odds_mock)
        monkeypatch.setattr(
            "src.odds_api_credits.credit_budget_check",
            mock.Mock(return_value=(True, "ok")),
        )
        result = client.get_game_odds("MLB", conn=mock.MagicMock())
        assert result is not None
        get_odds_mock.assert_called_once()


class TestOddsAPIPinnacleClientCreditBudget:
    def test_props_fetch_stops_when_budget_exhausted(self, monkeypatch):
        client = OddsAPIPinnacleClient()
        events = [{"id": f"evt{i}", "commence_time": "2026-08-26T20:00:00Z"} for i in range(3)]
        monkeypatch.setattr(client._client, "get_events", mock.Mock(return_value=(events, False)))
        get_event_odds_mock = mock.Mock(return_value=(_props_event(), False))
        monkeypatch.setattr(client._client, "get_event_odds", get_event_odds_mock)
        monkeypatch.setattr(
            "src.odds_api_credits.credit_budget_check",
            mock.Mock(side_effect=[(True, "ok"), (False, "out of budget")]),
        )
        fake_conn = mock.MagicMock()
        client.get_player_props("MLB", conn=fake_conn)
        assert get_event_odds_mock.call_count == 1

    def test_no_conn_skips_budget_check_but_still_fetches(self, monkeypatch):
        client = OddsAPIPinnacleClient()
        events = [{"id": "evt1", "commence_time": "2026-08-26T20:00:00Z"}]
        monkeypatch.setattr(client._client, "get_events", mock.Mock(return_value=(events, False)))
        monkeypatch.setattr(
            client._client, "get_event_odds",
            mock.Mock(return_value=(_props_event(), False)),
        )
        props = client.get_player_props("MLB", conn=None)
        assert props is not None and len(props) == 1


class TestOddsAPIPinnacleClientHasNoStandaloneThrottle:
    """Real methodological requirement (2026-08-26, operator directive):
    the offered sportsbook price and its Pinnacle reference must come
    from approximately the same point in time, or the resulting "edge"
    can be an artifact of the two prices moving apart, not a real
    inefficiency. A standalone multi-hour Pinnacle-specific throttle
    (tried, then reverted the same day) violated this directly. This
    client must never independently decide "too soon, skip" — the only
    thing that gates how often it's called is now the CALLER
    (src/player_prop_scanner.py gates props on fetch_props, matching
    the already-budget-tuned props-scan schedule; game odds are fetched
    on every scan, unthrottled, since that's cheap and matches when the
    comparison books themselves are fetched)."""

    def test_game_odds_is_fetched_fresh_even_with_a_very_recent_prior_fetch_on_record(self, tmp_path, monkeypatch):
        from database.db_manager import init_db, get_connection
        from datetime import datetime, timezone
        db_path = tmp_path / "no_throttle.db"
        init_db(str(db_path))
        conn = get_connection(str(db_path))
        conn.execute(
            "INSERT INTO odds_api_credits (recorded_at, endpoint, job_type, cache_hit) "
            "VALUES (?, 'odds_pinnacle_game', 'mlb_pinnacle_game_odds', 0)",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()

        client = OddsAPIPinnacleClient()
        get_odds_mock = mock.Mock(return_value=([_game_odds_event()], False))
        monkeypatch.setattr(client._client, "get_odds", get_odds_mock)

        result = client.get_game_odds("MLB", conn=conn)

        assert result is not None
        get_odds_mock.assert_called_once()

    def test_props_is_fetched_fresh_even_with_a_very_recent_prior_fetch_on_record(self, tmp_path, monkeypatch):
        from database.db_manager import init_db, get_connection
        from datetime import datetime, timezone
        db_path = tmp_path / "no_throttle2.db"
        init_db(str(db_path))
        conn = get_connection(str(db_path))
        conn.execute(
            "INSERT INTO odds_api_credits (recorded_at, endpoint, job_type, cache_hit) "
            "VALUES (?, 'odds_pinnacle_props', 'mlb_pinnacle_props', 0)",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()

        client = OddsAPIPinnacleClient()
        get_events_mock = mock.Mock(return_value=([{"id": "evt1", "commence_time": "2026-08-26T20:00:00Z"}], False))
        monkeypatch.setattr(client._client, "get_events", get_events_mock)
        monkeypatch.setattr(client._client, "get_event_odds", mock.Mock(return_value=(_props_event(), False)))

        result = client.get_player_props("MLB", conn=conn)

        assert result is not None
        get_events_mock.assert_called_once()


def _ou_group(
    market_type="batting_homeRuns_ou", event_id="evt-1",
    over_books=None, under_books=None, player_id="p1",
):
    return {
        "player_id": player_id, "event_id": event_id, "market_type": market_type,
        "over": over_books if over_books is not None else {"draftkings": {"price": -110}},
        "under": under_books if under_books is not None else {"draftkings": {"price": -110}},
    }


class TestDerivePropsTargets:
    """The selective-fetch optimization (2026-08-26): only request
    Pinnacle for event/market combinations THIS scan's own
    comparison-book data shows are genuinely evaluable, instead of every
    near-term event x every registered market regardless of whether
    there's real data to compare against."""

    FUTURE = "2099-01-01T00:00:00Z"
    PAST = "2020-01-01T00:00:00Z"

    def test_group_with_real_paired_data_and_upcoming_event_is_included(self):
        groups = {"k1": _ou_group(event_id="evt-1")}
        event_map = {"evt-1": {"start_time": self.FUTURE}}
        targets = derive_props_targets(groups, event_map, "MLB", min_books=1)
        assert targets == {"evt-1": {"batter_home_runs"}}

    def test_group_with_no_books_on_one_side_is_excluded(self):
        groups = {"k1": _ou_group(event_id="evt-1", under_books={})}
        event_map = {"evt-1": {"start_time": self.FUTURE}}
        targets = derive_props_targets(groups, event_map, "MLB", min_books=1)
        assert targets == {}

    def test_unregistered_market_type_is_excluded(self):
        groups = {"k1": _ou_group(event_id="evt-1", market_type="pitching_walks_ou")}
        event_map = {"evt-1": {"start_time": self.FUTURE}}
        targets = derive_props_targets(groups, event_map, "MLB", min_books=1)
        assert targets == {}

    def test_already_started_event_is_excluded(self):
        groups = {"k1": _ou_group(event_id="evt-1")}
        event_map = {"evt-1": {"start_time": self.PAST}}
        targets = derive_props_targets(groups, event_map, "MLB", min_books=1)
        assert targets == {}

    def test_game_market_group_is_excluded_handled_separately(self):
        groups = {"k1": _ou_group(event_id="evt-1", player_id="GAME", market_type="game_moneyline")}
        event_map = {"evt-1": {"start_time": self.FUTURE}}
        targets = derive_props_targets(groups, event_map, "MLB", min_books=1)
        assert targets == {}

    def test_missing_event_map_entry_is_treated_as_no_known_start_time_not_excluded(self):
        """A group referencing an event not in event_map (shouldn't
        normally happen, but must fail safe rather than silently drop a
        genuinely evaluable market)."""
        groups = {"k1": _ou_group(event_id="evt-unknown")}
        targets = derive_props_targets(groups, {}, "MLB", min_books=1)
        assert targets == {"evt-unknown": {"batter_home_runs"}}

    def test_multiple_eligible_markets_for_the_same_event_are_all_included(self):
        groups = {
            "k1": _ou_group(event_id="evt-1", market_type="batting_homeRuns_ou"),
            "k2": _ou_group(event_id="evt-1", market_type="pitching_strikeouts_ou"),
        }
        event_map = {"evt-1": {"start_time": self.FUTURE}}
        targets = derive_props_targets(groups, event_map, "MLB", min_books=1)
        assert targets == {"evt-1": {"batter_home_runs", "pitcher_strikeouts"}}

    def test_never_filters_on_price_or_apparent_edge_only_on_data_availability(self):
        """Real requirement (2026-08-26 operator directive): must not
        only fetch Pinnacle for bets that already look +EV via LOO, or
        real Pinnacle-revealed edges could be missed. Two groups with
        identical real book coverage but wildly different prices (one
        looking like a huge edge, one looking like none at all) must
        both be included — the function doesn't even look at price."""
        groups = {
            "huge_apparent_edge": _ou_group(
                event_id="evt-1", market_type="batting_homeRuns_ou",
                over_books={"draftkings": {"price": 900}},  # implies a huge edge
                under_books={"draftkings": {"price": -110}},
            ),
            "no_apparent_edge": _ou_group(
                event_id="evt-2", market_type="pitching_strikeouts_ou",
                over_books={"draftkings": {"price": -110}},
                under_books={"draftkings": {"price": -110}},
            ),
        }
        event_map = {
            "evt-1": {"start_time": self.FUTURE},
            "evt-2": {"start_time": self.FUTURE},
        }
        targets = derive_props_targets(groups, event_map, "MLB", min_books=1)
        assert targets == {
            "evt-1": {"batter_home_runs"},
            "evt-2": {"pitcher_strikeouts"},
        }
