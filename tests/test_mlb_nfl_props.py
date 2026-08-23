"""Tests for MLB/NFL's supplemental player-props source via The Odds API
(added 2026-08-22 — see src/mlb_props_parser.py and src/nfl_props_parser.py's
docstrings for the real live liquidity check this was built from).

The row-building/identity-resolution mechanics are already covered
thoroughly by tests/test_wnba_odds.py (all three leagues share the same
core via src/odds_api_props_parser.py) — these tests focus on what's
actually different: the market-key-to-market_type mapping, correct
delegation from each league's fetch/parse wrapper to the shared modules,
and the new run_scan()/worker.py wiring that lets MLB/NFL (whose primary
provider IS SportsGameOdds, unlike WNBA) also merge in this supplemental
source.
"""

from __future__ import annotations

from unittest import mock

import pytest


class FakeRosterClientForProps:
    """Minimal fake satisfying resolve_player_identity's client interface."""

    def __init__(self, teams: dict[str, str], rosters: dict[str, list]):
        self._teams = teams
        self._rosters = rosters

    def find_team_id(self, league, team_display_name):
        return self._teams.get(team_display_name)

    def get_roster(self, league, team_id):
        return self._rosters.get(team_id, [])


def _mlb_event_odds_with_props(
    player_name="Mitch Bratt", home_team="Arizona Diamondbacks",
    away_team="Houston Astros", event_id="evt-1", market_key="batter_home_runs",
):
    """Shaped like GET /v4/sports/baseball_mlb/events/{id}/odds — same
    outcome shape verified live 2026-08-22 (outcome.description carries
    the player's name)."""
    return {
        "id": event_id,
        "home_team": home_team,
        "away_team": away_team,
        "bookmakers": [
            {
                "key": "draftkings",
                "last_update": "2026-08-22T23:00:00Z",
                "markets": [
                    {
                        "key": market_key,
                        "last_update": "2026-08-22T23:00:00Z",
                        "outcomes": [
                            {"name": "Over", "description": player_name, "price": -120, "point": 0.5},
                            {"name": "Under", "description": player_name, "price": 100, "point": 0.5},
                        ],
                    },
                ],
            },
        ],
    }


def _nfl_event_odds_with_props(
    player_name="Isaiah Adams", home_team="Seattle Seahawks",
    away_team="New England Patriots", event_id="evt-1", market_key="player_pass_yds",
):
    return {
        "id": event_id,
        "home_team": home_team,
        "away_team": away_team,
        "bookmakers": [
            {
                "key": "fanduel",
                "last_update": "2026-08-22T23:00:00Z",
                "markets": [
                    {
                        "key": market_key,
                        "last_update": "2026-08-22T23:00:00Z",
                        "outcomes": [
                            {"name": "Over", "description": player_name, "price": -114, "point": 229.5},
                            {"name": "Under", "description": player_name, "price": -106, "point": 229.5},
                        ],
                    },
                ],
            },
        ],
    }


class TestMLBPropsMarketMapping:
    def test_registered_markets_reuse_mlb_primary_registry_naming(self):
        from src.mlb_props_parser import _PROP_MARKET_TYPE
        # Exact strings from src/prop_config.py's PITCHER_STRIKEOUTS/
        # PITCHER_OUTS/BATTER_HOME_RUNS/BATTER_TOTAL_BASES — reusing them
        # (not inventing new ones) is what makes MLB's existing settlement
        # contract apply automatically. batter_home_runs (not
        # batter_hits, swapped 2026-08-23) specifically because it's one
        # of Pinnacle's 6 real covered MLB stats — batting_hits_ou isn't.
        assert _PROP_MARKET_TYPE == {
            "batter_home_runs": "batting_homeRuns_ou",
            "batter_total_bases": "batting_totalBases_ou",
            "pitcher_strikeouts": "pitching_strikeouts_ou",
            "pitcher_outs": "pitching_outs_ou",
        }

    def test_all_four_markets_are_auto_settleable(self):
        from src.mlb_props_parser import _PROP_MARKET_TYPE
        from src.prop_config import AUTO_SETTLEABLE_MARKET_TYPES
        for market_type in _PROP_MARKET_TYPE.values():
            assert market_type in AUTO_SETTLEABLE_MARKET_TYPES

    def test_parse_mlb_player_props_resolves_and_maps_market_type(self, tmp_path):
        from database.db_manager import init_db, get_connection
        from src.mlb_props_parser import parse_mlb_player_props
        from src.player_identity import RosterPlayer, normalize_name

        db_path = tmp_path / "mlb_props.db"
        init_db(str(db_path))
        conn = get_connection(str(db_path))

        roster_client = FakeRosterClientForProps(
            teams={"Arizona Diamondbacks": "29", "Houston Astros": "18"},
            rosters={"29": [RosterPlayer("5123768", "Mitch Bratt",
                                          normalize_name("Mitch Bratt"), "29", "Arizona Diamondbacks")]},
        )
        result = parse_mlb_player_props(
            [_mlb_event_odds_with_props()], conn=conn, roster_client=roster_client,
        )
        assert len(result.odds_rows) == 2
        assert all(r["market_type"] == "batting_homeRuns_ou" for r in result.odds_rows)
        assert all(r["player_id"] == "ESPN_MLB_5123768" for r in result.odds_rows)

    def test_unregistered_market_key_ignored(self, tmp_path):
        """A market key not in the 4-item registry (e.g. one of the
        thinner ones deliberately left out — see the module docstring)
        must be silently skipped, not guessed into some market_type."""
        from database.db_manager import init_db, get_connection
        from src.mlb_props_parser import parse_mlb_player_props

        db_path = tmp_path / "mlb_props2.db"
        init_db(str(db_path))
        conn = get_connection(str(db_path))

        # batter_hits specifically -- no longer registered as of 2026-08-23
        # (swapped for batter_home_runs, see module docstring), so this
        # doubles as a regression test that the swap actually took.
        event_odds = _mlb_event_odds_with_props(market_key="batter_hits")
        result = parse_mlb_player_props(
            [event_odds], conn=conn, roster_client=FakeRosterClientForProps({}, {}),
        )
        assert result.odds_rows == []
        assert result.audit_rows == []


class TestNFLPropsMarketMapping:
    def test_registered_markets_reuse_nfl_primary_registry_naming(self):
        from src.nfl_props_parser import _PROP_MARKET_TYPE
        assert _PROP_MARKET_TYPE == {
            "player_pass_yds": "passing_yards_ou",
            "player_rush_yds": "rushing_yards_ou",
            "player_receptions": "receiving_receptions_ou",
            "player_reception_yds": "receiving_yards_ou",
        }

    def test_anytime_td_not_registered(self):
        """player_anytime_td is single-sided "Yes" pricing on this
        provider (verified live 2026-08-22), not genuine Over/Under —
        this generic parser can't handle it correctly, so it's
        deliberately excluded rather than silently producing broken rows."""
        from src.nfl_props_parser import _PROP_MARKET_TYPE
        assert "player_anytime_td" not in _PROP_MARKET_TYPE

    def test_all_four_markets_have_settlement_field_mapping(self):
        from src.nfl_props_parser import _PROP_MARKET_TYPE
        from src.nfl_results import _SIMPLE_STAT_FIELDS
        for market_type in _PROP_MARKET_TYPE.values():
            base = market_type.removesuffix("_ou")
            assert base in _SIMPLE_STAT_FIELDS, f"{base} missing from NFL settlement field map"

    def test_parse_nfl_player_props_resolves_and_maps_market_type(self, tmp_path):
        from database.db_manager import init_db, get_connection
        from src.nfl_props_parser import parse_nfl_player_props
        from src.player_identity import RosterPlayer, normalize_name

        db_path = tmp_path / "nfl_props.db"
        init_db(str(db_path))
        conn = get_connection(str(db_path))

        roster_client = FakeRosterClientForProps(
            teams={"Seattle Seahawks": "26", "New England Patriots": "17"},
            rosters={"26": [RosterPlayer("5084939", "Isaiah Adams",
                                          normalize_name("Isaiah Adams"), "26", "Seattle Seahawks")]},
        )
        result = parse_nfl_player_props(
            [_nfl_event_odds_with_props()], conn=conn, roster_client=roster_client,
        )
        assert len(result.odds_rows) == 2
        assert all(r["market_type"] == "passing_yards_ou" for r in result.odds_rows)
        assert all(r["player_id"] == "ESPN_NFL_5084939" for r in result.odds_rows)


class TestFetchPlayerPropsViaOddsAPIDelegation:
    """fetch_player_props_via_odds_api() on each league module must
    delegate to the shared src.odds_api_props_fetch.fetch_player_props
    with the right sport_key/market_keys/parser/league — not re-implement
    the fetch loop."""

    def test_mlb_delegates_with_correct_args(self, tmp_path):
        from database.db_manager import init_db, get_connection
        from src.sports import mlb as mlb_mod

        db_path = tmp_path / "mlb_deleg.db"
        init_db(str(db_path))
        conn = get_connection(str(db_path))

        with mock.patch("src.odds_api_props_fetch.fetch_player_props",
                         return_value=([{"r": 1}], [{"a": 1}])) as mock_fetch:
            result = mlb_mod.fetch_player_props_via_odds_api(conn, event_id="evt-x")

        assert result == ([{"r": 1}], [{"a": 1}])
        _, kwargs = mock_fetch.call_args
        assert kwargs["sport_key"] == "baseball_mlb"
        assert kwargs["league"] == "MLB"
        assert kwargs["event_id"] == "evt-x"
        assert "batter_home_runs" in kwargs["prop_market_keys"]

    def test_nfl_delegates_with_correct_args(self, tmp_path):
        from database.db_manager import init_db, get_connection
        from src.sports import nfl as nfl_mod

        db_path = tmp_path / "nfl_deleg.db"
        init_db(str(db_path))
        conn = get_connection(str(db_path))

        with mock.patch("src.odds_api_props_fetch.fetch_player_props",
                         return_value=([], [])) as mock_fetch:
            nfl_mod.fetch_player_props_via_odds_api(conn)

        _, kwargs = mock_fetch.call_args
        assert kwargs["sport_key"] == "americanfootball_nfl"
        assert kwargs["league"] == "NFL"
        assert kwargs["event_id"] is None
        assert "player_pass_yds" in kwargs["prop_market_keys"]


class TestRunScanMergesSupplementalPropsForSGOLeagues:
    """MLB/NFL's primary provider IS SportsGameOdds, unlike WNBA — the
    fetch_props merge added 2026-08-22 must work from INSIDE the
    SportsGameOdds branch, not just the non-SGO (WNBA-style) branch."""

    def test_mlb_fetch_props_true_merges_supplemental_rows(self):
        from src import player_prop_scanner as scanner

        sgo_events = {"data": [{"eventID": "e1", "odds": {}}]}
        with mock.patch.object(scanner, "get_connection", return_value=mock.MagicMock()), \
             mock.patch.object(scanner, "create_run", return_value="run-1"), \
             mock.patch.object(scanner, "save_player_prop_batch"), \
             mock.patch.object(scanner.SportsGameOddsClient, "get_events",
                                return_value=(sgo_events, False)), \
             mock.patch.object(scanner, "parse_player_props",
                                return_value=mock.MagicMock(odds_rows=[], audit_rows=[])), \
             mock.patch("src.sports.mlb.fetch_player_props_via_odds_api",
                         return_value=([], [])) as mock_props:
            result = scanner.run_scan(
                mode="all", market="all", market_form="all", league="MLB", fetch_props=True,
            )

        mock_props.assert_called_once()
        assert result["n_events"] == 1

    def test_mlb_fetch_props_false_does_not_call_odds_api(self):
        from src import player_prop_scanner as scanner

        sgo_events = {"data": [{"eventID": "e1", "odds": {}}]}
        with mock.patch.object(scanner, "get_connection", return_value=mock.MagicMock()), \
             mock.patch.object(scanner, "create_run", return_value="run-1"), \
             mock.patch.object(scanner, "save_player_prop_batch"), \
             mock.patch.object(scanner.SportsGameOddsClient, "get_events",
                                return_value=(sgo_events, False)), \
             mock.patch.object(scanner, "parse_player_props",
                                return_value=mock.MagicMock(odds_rows=[], audit_rows=[])), \
             mock.patch("src.sports.mlb.fetch_player_props_via_odds_api") as mock_props:
            scanner.run_scan(mode="all", market="all", market_form="all", league="MLB", fetch_props=False)

        mock_props.assert_not_called()

    def test_props_fetch_failure_does_not_break_the_scan(self):
        """A real failure in the supplemental Odds-API props fetch must
        not take down the whole scan — SportsGameOdds's own props already
        came back fine, this is purely additive."""
        from src import player_prop_scanner as scanner

        sgo_events = {"data": [{"eventID": "e1", "odds": {}}]}
        with mock.patch.object(scanner, "get_connection", return_value=mock.MagicMock()), \
             mock.patch.object(scanner, "create_run", return_value="run-1"), \
             mock.patch.object(scanner, "save_player_prop_batch"), \
             mock.patch.object(scanner.SportsGameOddsClient, "get_events",
                                return_value=(sgo_events, False)), \
             mock.patch.object(scanner, "parse_player_props",
                                return_value=mock.MagicMock(odds_rows=[], audit_rows=[])), \
             mock.patch("src.sports.mlb.fetch_player_props_via_odds_api",
                         side_effect=RuntimeError("boom")):
            result = scanner.run_scan(
                mode="all", market="all", market_form="all", league="MLB", fetch_props=True,
            )

        assert result["n_events"] == 1  # did not raise

    def test_league_without_the_hook_is_ignored(self):
        """A league with no fetch_player_props_via_odds_api (e.g. one
        that hasn't gotten this build yet) must not error when
        fetch_props=True is passed — same graceful-getattr pattern as
        the game-odds fallback."""
        from src import player_prop_scanner as scanner
        from unittest import mock as _mock

        fake_league = _mock.MagicMock(spec=["get_market_registry", "ODDS_PROVIDER"])
        fake_league.ODDS_PROVIDER = "sportsgameodds"
        del fake_league.fetch_player_props_via_odds_api

        sgo_events = {"data": [{"eventID": "e1", "odds": {}}]}
        with mock.patch.object(scanner, "get_connection", return_value=mock.MagicMock()), \
             mock.patch.object(scanner, "create_run", return_value="run-1"), \
             mock.patch.object(scanner, "save_player_prop_batch"), \
             mock.patch.object(scanner.SportsGameOddsClient, "get_events",
                                return_value=(sgo_events, False)), \
             mock.patch.object(scanner, "parse_player_props",
                                return_value=mock.MagicMock(odds_rows=[], audit_rows=[])), \
             mock.patch("src.sports.get_league", return_value=fake_league):
            result = scanner.run_scan(
                mode="all", market="all", market_form="all", league="FAKE", fetch_props=True,
            )

        assert result["n_events"] == 1


class TestFetchPlayerPropsFiltersToNearTermEvents:
    """Real bug, found live 2026-08-22 testing NFL props end-to-end:
    get_events() returns EVERY event currently listed for the sport --
    for NFL that's the entire season (272 games), not the near-term
    slate. Without a filter, fetch_player_props() would spend
    credit-budget-check cycles and real API calls working through games
    months away. Reproduced live (multi-minute hang before the fix,
    finished immediately after) and now covered here with a fast,
    deterministic mock."""

    def test_far_future_events_excluded_from_the_fetch_loop(self, tmp_path):
        from datetime import datetime, timedelta, timezone
        from database.db_manager import init_db, get_connection
        from src.odds_api_props_fetch import fetch_player_props

        db_path = tmp_path / "props_window.db"
        init_db(str(db_path))
        conn = get_connection(str(db_path))

        now = datetime.now(timezone.utc)
        near_term = {"id": "evt-near", "commence_time": (now + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        far_future = {"id": "evt-far", "commence_time": (now + timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")}

        fake_client = mock.MagicMock()
        fake_client.get_events.return_value = ([near_term, far_future], False)
        fake_client.get_event_odds.return_value = ({"id": "evt-near", "bookmakers": []}, False)
        fake_client.last_quota = {}

        with mock.patch("src.odds_api_client.OddsAPIClient", return_value=fake_client), \
             mock.patch("src.player_identity.ESPNRosterClient"):
            fetch_player_props(
                conn, sport_key="americanfootball_nfl", prop_market_keys="player_pass_yds",
                parse_fn=lambda *a, **k: mock.MagicMock(odds_rows=[], audit_rows=[]), league="NFL",
            )

        fake_client.get_event_odds.assert_called_once()
        called_event_id = fake_client.get_event_odds.call_args[0][0]
        assert called_event_id == "evt-near"

    def test_explicit_event_id_bypasses_the_window_filter(self, tmp_path):
        """A manual/targeted re-check for a specific event must always
        fetch it, even if it's outside the near-term window (mirrors the
        existing dedup-bypass behavior for explicit event_id requests)."""
        from datetime import datetime, timedelta, timezone
        from database.db_manager import init_db, get_connection
        from src.odds_api_props_fetch import fetch_player_props

        db_path = tmp_path / "props_window2.db"
        init_db(str(db_path))
        conn = get_connection(str(db_path))

        now = datetime.now(timezone.utc)
        far_future = {"id": "evt-far", "commence_time": (now + timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")}

        fake_client = mock.MagicMock()
        fake_client.get_events.return_value = ([far_future], False)
        fake_client.get_event_odds.return_value = ({"id": "evt-far", "bookmakers": []}, False)
        fake_client.last_quota = {}

        with mock.patch("src.odds_api_client.OddsAPIClient", return_value=fake_client):
            fetch_player_props(
                conn, sport_key="americanfootball_nfl", prop_market_keys="player_pass_yds",
                parse_fn=lambda *a, **k: mock.MagicMock(odds_rows=[], audit_rows=[]), league="NFL",
                event_id="evt-far",
            )

        fake_client.get_event_odds.assert_called_once()
