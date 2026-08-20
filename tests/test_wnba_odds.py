"""Tests for the WNBA odds path (The Odds API — a different provider than
SportsGameOdds, used only for WNBA).

Fixtures are synthetic but shaped exactly like the real The Odds API v4
response, verified live 2026-08-19 (see src/odds_api_client.py and
src/wnba_odds_parser.py docstrings for the exact endpoints/fields
checked). No test makes a live network call — same discipline as every
other test in this suite.
"""

from __future__ import annotations

from unittest import mock

import pytest

from src.wnba_odds_parser import parse_wnba_game_odds

# Imported at module level (not inside the missing-key test below) so its
# module-level load_dotenv() has already run by the time that test deletes
# THE_ODDS_API_KEY from os.environ — otherwise a first-time import inside
# the test would call load_dotenv() *after* the delete and silently
# re-populate the var from the real .env file, since load_dotenv() only
# fills in variables that are currently absent.
import src.odds_api_client  # noqa: F401


def _game(
    game_id="c162d60ca8c8caf4195c3abf4566747a",
    home_team="Washington Mystics",
    away_team="Toronto Tempo",
    spread_point=13.5,
):
    """Shaped like one entry in GET /v4/sports/basketball_wnba/odds — verified
    field names/nesting against a real live response."""
    return {
        "id": game_id,
        "sport_key": "basketball_wnba",
        "sport_title": "WNBA",
        "commence_time": "2026-08-19T23:30:00Z",
        "home_team": home_team,
        "away_team": away_team,
        "bookmakers": [
            {
                "key": "fanduel",
                "title": "FanDuel",
                "last_update": "2026-08-19T19:45:26Z",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": "2026-08-19T19:45:26Z",
                        "outcomes": [
                            {"name": away_team, "price": 650},
                            {"name": home_team, "price": -1100},
                        ],
                    },
                    {
                        "key": "spreads",
                        "last_update": "2026-08-19T19:45:26Z",
                        "outcomes": [
                            {"name": away_team, "price": -110, "point": spread_point},
                            {"name": home_team, "price": -110, "point": -spread_point},
                        ],
                    },
                    {
                        "key": "totals",
                        "last_update": "2026-08-19T19:45:26Z",
                        "outcomes": [
                            {"name": "Over", "price": -110, "point": 168.5},
                            {"name": "Under", "price": -110, "point": 168.5},
                        ],
                    },
                ],
            },
            {
                "key": "draftkings",
                "title": "DraftKings",
                "last_update": "2026-08-19T19:44:00Z",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": "2026-08-19T19:44:00Z",
                        "outcomes": [
                            {"name": away_team, "price": 620},
                            {"name": home_team, "price": -950},
                        ],
                    },
                ],
            },
        ],
    }


class TestParseWNBAGameOdds:
    def test_parses_h2h_spreads_totals(self):
        result = parse_wnba_game_odds([_game()])
        market_types = {r["market_type"] for r in result.odds_rows}
        assert market_types == {"game_moneyline", "game_spread_ou", "game_total_ou"}
        # fanduel: h2h(2) + spreads(2) + totals(2) = 6; draftkings: h2h(2) = 2
        assert len(result.odds_rows) == 8
        assert result.audit_rows and all(not r["excluded"] for r in result.audit_rows)

    def test_h2h_side_resolves_to_away_home(self):
        result = parse_wnba_game_odds([_game()])
        ml_rows = [r for r in result.odds_rows if r["market_type"] == "game_moneyline"]
        away_rows = [r for r in ml_rows if r["side"] == "AWAY"]
        home_rows = [r for r in ml_rows if r["side"] == "HOME"]
        assert away_rows and all(r["team_name"] == "Toronto Tempo" for r in away_rows)
        assert home_rows and all(r["team_name"] == "Washington Mystics" for r in home_rows)
        assert all(r["line"] is None for r in ml_rows)

    def test_spread_line_is_abs_valued_and_pairs_away_home(self):
        result = parse_wnba_game_odds([_game(spread_point=13.5)])
        spread_rows = [r for r in result.odds_rows
                        if r["market_type"] == "game_spread_ou" and r["sportsbook"] == "fanduel"]
        assert len(spread_rows) == 2
        assert all(r["line"] == 13.5 for r in spread_rows)
        # Same market_group_key means the scanner will pair them as one group.
        assert spread_rows[0]["market_group_key"] == spread_rows[1]["market_group_key"]

    def test_totals_side_is_over_under(self):
        result = parse_wnba_game_odds([_game()])
        total_rows = [r for r in result.odds_rows
                       if r["market_type"] == "game_total_ou" and r["sportsbook"] == "fanduel"]
        sides = {r["side"] for r in total_rows}
        assert sides == {"OVER", "UNDER"}
        assert all(r["line"] == 168.5 for r in total_rows)

    def test_decimal_odds_computed_correctly(self):
        result = parse_wnba_game_odds([_game()])
        row = next(r for r in result.odds_rows
                   if r["market_type"] == "game_moneyline" and r["sportsbook"] == "fanduel"
                   and r["side"] == "AWAY")
        assert row["price"] == 650
        assert row["decimal_odds"] == pytest.approx(7.5, abs=0.001)

    def test_unrecognized_team_name_is_excluded_not_guessed(self):
        game = _game()
        game["bookmakers"][0]["markets"][0]["outcomes"][0]["name"] = "Some Other Team"
        result = parse_wnba_game_odds([game])
        excluded = [r for r in result.audit_rows if r["excluded"]]
        assert any("Could not resolve side" in r["exclusion_reasons"] for r in excluded)

    def test_missing_price_is_excluded(self):
        game = _game()
        del game["bookmakers"][0]["markets"][0]["outcomes"][0]["price"]
        result = parse_wnba_game_odds([game])
        excluded = [r for r in result.audit_rows if r["excluded"]]
        assert any("Missing or invalid price" in r["exclusion_reasons"] for r in excluded)

    def test_unknown_market_key_is_ignored(self):
        game = _game()
        game["bookmakers"][0]["markets"].append({
            "key": "alternate_spreads", "last_update": "2026-08-19T19:45:26Z",
            "outcomes": [{"name": "Toronto Tempo", "price": 100, "point": 10.5}],
        })
        result = parse_wnba_game_odds([game])
        assert all(r["market_type"] != "alternate_spreads" for r in result.odds_rows)

    def test_empty_games_list(self):
        result = parse_wnba_game_odds([])
        assert result.odds_rows == []
        assert result.audit_rows == []

    def test_two_games_produce_distinct_group_keys(self):
        g1 = _game(game_id="game-1")
        g2 = _game(game_id="game-2", home_team="Seattle Storm", away_team="Chicago Sky")
        result = parse_wnba_game_odds([g1, g2])
        keys = {r["market_group_key"] for r in result.odds_rows}
        assert all("game-1" in k or "game-2" in k for k in keys)
        assert len({k.split("|")[0] for k in keys}) == 2


class TestOddsAPIClient:
    def test_missing_key_raises_clear_error(self, monkeypatch):
        monkeypatch.delenv("THE_ODDS_API_KEY", raising=False)
        from src.odds_api_client import OddsAPIClient, OddsAPIKeyError
        with pytest.raises(OddsAPIKeyError):
            OddsAPIClient()

    def test_explicit_key_bypasses_env_lookup(self, monkeypatch):
        monkeypatch.delenv("THE_ODDS_API_KEY", raising=False)
        from src.odds_api_client import OddsAPIClient
        client = OddsAPIClient(api_key="explicit-key", cache_dir="data/_test_odds_api_cache")
        assert client.api_key == "explicit-key"

    def test_get_odds_uses_correct_credit_formula_params(self, tmp_path, monkeypatch):
        """markets x regions is the documented credit formula; assert we send
        exactly the params that formula is billed on."""
        from src.odds_api_client import OddsAPIClient
        client = OddsAPIClient(api_key="test-key", cache_dir=str(tmp_path))

        captured = {}

        class FakeResponse:
            status_code = 200
            headers = {}

            def json(self):
                return [{"id": "g1"}]

            def raise_for_status(self):
                pass

        def fake_get(url, params=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            return FakeResponse()

        monkeypatch.setattr(client.session, "get", fake_get)
        client.get_odds(sport_key="basketball_wnba", regions="us", markets="h2h,spreads,totals")
        assert captured["params"]["regions"] == "us"
        assert captured["params"]["markets"] == "h2h,spreads,totals"
        assert captured["params"]["apiKey"] == "test-key"
        assert "basketball_wnba/odds" in captured["url"]


class TestWNBASportsAdapter:
    def test_fetch_and_parse_normalizes_events_for_build_event_map(self):
        from src.sports import wnba as wnba_mod
        from src.player_prop_scanner import _build_event_map

        fake_games = [_game()]
        with mock.patch("src.odds_api_client.OddsAPIClient") as MockClient:
            MockClient.return_value.get_odds.return_value = (fake_games, False)
            odds_rows, audit_rows, events, from_cache = wnba_mod.fetch_and_parse()

        assert from_cache is False
        assert len(odds_rows) == 8
        event_map = _build_event_map(events)
        eid = fake_games[0]["id"]
        assert event_map[eid]["away_name"] == "Toronto Tempo"
        assert event_map[eid]["home_name"] == "Washington Mystics"
        assert event_map[eid]["start_time"] == "2026-08-19T23:30:00Z"

    def test_fetch_and_parse_filters_by_event_id(self):
        from src.sports import wnba as wnba_mod
        g1 = _game(game_id="game-1")
        g2 = _game(game_id="game-2", home_team="Seattle Storm", away_team="Chicago Sky")
        with mock.patch("src.odds_api_client.OddsAPIClient") as MockClient:
            MockClient.return_value.get_odds.return_value = ([g1, g2], False)
            odds_rows, audit_rows, events, from_cache = wnba_mod.fetch_and_parse(event_id="game-2")
        assert len(events) == 1
        assert events[0]["id"] == "game-2"
        assert all(r["event_id"] == "game-2" for r in odds_rows)


class TestRunScanWNBA:
    def test_run_scan_league_wnba_produces_opportunities(self):
        """Full run_scan(league='WNBA', ...) through the real generic
        scanner/analysis pipeline, only the odds-fetch mocked."""
        from src import player_prop_scanner as scanner

        fake_games = [_game()]
        with mock.patch.object(scanner, "get_connection", return_value=mock.MagicMock()), \
             mock.patch.object(scanner, "create_run", return_value="run-1"), \
             mock.patch.object(scanner, "save_player_prop_batch"), \
             mock.patch("src.odds_api_client.OddsAPIClient") as MockClient:
            MockClient.return_value.get_odds.return_value = (fake_games, False)
            result = scanner.run_scan(mode="all", market="all", market_form="all", league="WNBA")

        assert result["n_events"] == 1
        opp_market_types = {o["market_type"] for o in result["opportunities"]}
        assert opp_market_types & {"game_moneyline", "game_spread_ou", "game_total_ou"}


# ───────────────────────────────────────────────────────────────────
# Player props — routed through identity resolution
# ───────────────────────────────────────────────────────────────────

def _event_odds_with_props(player_name="Shakira Austin", home_team="Washington Mystics",
                            away_team="Toronto Tempo", event_id="evt-1"):
    """Shaped like GET /v4/sports/basketball_wnba/events/{id}/odds — verified
    live 2026-08-19 (outcome.description carries the player's name, not a
    stable ID)."""
    return {
        "id": event_id,
        "home_team": home_team,
        "away_team": away_team,
        "bookmakers": [
            {
                "key": "fanduel",
                "last_update": "2026-08-19T19:45:26Z",
                "markets": [
                    {
                        "key": "player_points",
                        "last_update": "2026-08-19T19:45:26Z",
                        "outcomes": [
                            {"name": "Over", "description": player_name, "price": -115, "point": 14.5},
                            {"name": "Under", "description": player_name, "price": -105, "point": 14.5},
                        ],
                    },
                ],
            },
        ],
    }


class FakeRosterClientForProps:
    """Minimal fake satisfying resolve_player_identity's client interface."""

    def find_team_id(self, league, team_display_name):
        return {"Washington Mystics": "20", "Toronto Tempo": "30"}.get(team_display_name)

    def get_roster(self, league, team_id):
        from src.player_identity import RosterPlayer, normalize_name
        if team_id == "20":
            return [RosterPlayer("4398911", "Shakira Austin",
                                  normalize_name("Shakira Austin"), "20", "Washington Mystics")]
        return []


def _prop_row(*, side, price, event_id="evt-1", player_id="4398911",
              player_name="Shakira Austin", line=14.5, mapping_confidence="HIGH",
              sportsbook="fanduel"):
    """A row shaped exactly like src.wnba_odds_parser._build_prop_row's
    output (validation_status/market_group_key/raw_line/mapping_confidence
    all present) — lets run_scan's grouping logic be exercised directly
    without a live fetch."""
    from src.player_prop_parser import _build_group_key
    return {
        "event_id": event_id,
        "odd_id": f"player_points-{event_id}-{player_name}-{side}-{sportsbook}",
        "sportsbook": sportsbook,
        "player_id": player_id,
        "player_name": player_name,
        "team_id": "", "team_name": "",
        "market_type": "player_points_ou",
        "market_group_key": _build_group_key(
            event_id, player_id, line, 0, side, "player_points_ou"
        ),
        "side": side,
        "line": line,
        "raw_line": line,
        "price": price,
        "decimal_odds": 1.9,
        "is_alt_line": 0,
        "available": 1,
        "validation_status": "VALID",
        "mapping_confidence": mapping_confidence,
        "mapping_method": "exact_normalized",
        "validation_reason": "OK",
        "captured_at": "2026-08-19T19:45:26+00:00",
        "observation_time": "2026-08-19T19:45:26+00:00",
    }


class TestRunScanWNBAPlayerProps:
    """mapping_confidence and raw_line (added for Priority 1/4 of this
    session's mandate) must survive run_scan's O/U grouping into the final
    opportunity dict — they're threaded through the same generic grouping
    code game markets use, so a regression there would silently drop
    both fields for every player-prop opportunity."""

    def test_mapping_confidence_and_raw_line_reach_opportunity(self):
        from src import player_prop_scanner as scanner
        from src.sports import wnba as wnba_mod

        rows = [
            _prop_row(side="over", price=-115, mapping_confidence="HIGH", sportsbook="fanduel"),
            _prop_row(side="under", price=-105, mapping_confidence="HIGH", sportsbook="fanduel"),
            _prop_row(side="over", price=-120, mapping_confidence="HIGH", sportsbook="draftkings"),
            _prop_row(side="under", price=-100, mapping_confidence="HIGH", sportsbook="draftkings"),
        ]
        events = [{"id": "evt-1", "status": {"startsAt": "2099-01-01T00:00:00Z"}}]

        with mock.patch.object(scanner, "get_connection", return_value=mock.MagicMock()), \
             mock.patch.object(scanner, "create_run", return_value="run-1"), \
             mock.patch.object(scanner, "save_player_prop_batch"), \
             mock.patch.object(wnba_mod, "fetch_and_parse",
                                return_value=(rows, [], events, False)):
            result = scanner.run_scan(mode="all", market="all", market_form="all",
                                       league="WNBA")

        props = [o for o in result["opportunities"] if o["market_type"] == "player_points_ou"]
        assert props, "expected at least one player_points_ou opportunity"
        for opp in props:
            assert opp["mapping_confidence"] == "HIGH"
            assert opp["raw_line"] == 14.5


class TestParseWNBAPlayerProps:
    def test_resolved_player_produces_approved_rows(self, tmp_path):
        from database.db_manager import init_db, get_connection
        from src.wnba_odds_parser import parse_wnba_player_props

        db_path = tmp_path / "props.db"
        init_db(str(db_path))
        conn = get_connection(str(db_path))

        result = parse_wnba_player_props(
            [_event_odds_with_props()], conn=conn, roster_client=FakeRosterClientForProps(),
        )
        assert len(result.odds_rows) == 2
        assert all(r["mapping_confidence"] == "HIGH" for r in result.odds_rows)
        assert all(r["player_id"] == "ESPN_WNBA_4398911" for r in result.odds_rows)
        assert {r["side"] for r in result.odds_rows} == {"OVER", "UNDER"}
        assert all(r["market_type"] == "player_points_ou" for r in result.odds_rows)

    def test_unresolved_player_is_excluded_not_guessed(self, tmp_path):
        from database.db_manager import init_db, get_connection
        from src.wnba_odds_parser import parse_wnba_player_props

        db_path = tmp_path / "props2.db"
        init_db(str(db_path))
        conn = get_connection(str(db_path))

        event_odds = _event_odds_with_props(player_name="Totally Unknown Player")
        result = parse_wnba_player_props(
            [event_odds], conn=conn, roster_client=FakeRosterClientForProps(),
        )
        assert result.odds_rows == []
        assert all(r["excluded"] for r in result.audit_rows)
        assert all("not trusted" in r["exclusion_reasons"] for r in result.audit_rows)

    def test_identity_resolution_is_cached_across_calls(self, tmp_path):
        """Second call for the same player must not re-invoke roster lookup —
        it should hit the DB-cached mapping instead."""
        from database.db_manager import init_db, get_connection
        from src.wnba_odds_parser import parse_wnba_player_props

        db_path = tmp_path / "props3.db"
        init_db(str(db_path))
        conn = get_connection(str(db_path))

        roster_client = mock.MagicMock(wraps=FakeRosterClientForProps())
        parse_wnba_player_props([_event_odds_with_props()], conn=conn, roster_client=roster_client)
        first_call_count = roster_client.get_roster.call_count
        assert first_call_count > 0

        # Second call, same player/team context: should be served from the
        # DB cache without calling the roster client again.
        parse_wnba_player_props(
            [_event_odds_with_props(event_id="evt-2")], conn=conn, roster_client=roster_client,
        )
        assert roster_client.get_roster.call_count == first_call_count

    def test_two_sides_share_market_group_key(self, tmp_path):
        from database.db_manager import init_db, get_connection
        from src.wnba_odds_parser import parse_wnba_player_props

        db_path = tmp_path / "props4.db"
        init_db(str(db_path))
        conn = get_connection(str(db_path))

        result = parse_wnba_player_props(
            [_event_odds_with_props()], conn=conn, roster_client=FakeRosterClientForProps(),
        )
        keys = {r["market_group_key"] for r in result.odds_rows}
        assert len(keys) == 1  # Over and Under paired into one group

    def test_missing_price_excluded(self, tmp_path):
        from database.db_manager import init_db, get_connection
        from src.wnba_odds_parser import parse_wnba_player_props

        db_path = tmp_path / "props5.db"
        init_db(str(db_path))
        conn = get_connection(str(db_path))

        event_odds = _event_odds_with_props()
        del event_odds["bookmakers"][0]["markets"][0]["outcomes"][0]["price"]
        result = parse_wnba_player_props(
            [event_odds], conn=conn, roster_client=FakeRosterClientForProps(),
        )
        excluded = [r for r in result.audit_rows if r["excluded"]]
        assert any("Missing or invalid price" in r["exclusion_reasons"] for r in excluded)
